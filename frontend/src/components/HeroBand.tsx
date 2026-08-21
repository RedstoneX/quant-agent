import {
  Badge,
  Card,
  CategoryBar,
  Legend,
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
      <Card className="!bg-panel !ring-border h-full">
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
    <Card decoration="top" decorationColor="violet" className="!bg-panel !ring-border h-full">
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
}: {
  account: AccountResponse | null;
  accountError: string | null;
  positions: PositionItem[];
  funnel: RunFunnelResponse | null;
}) {
  if (!account) {
    return (
      <Card className="mx-3 mt-3 !bg-panel !ring-border text-center">
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
  const longPct = total > 0 ? (longMv / total) * 100 : 0;
  const hedgePct = total > 0 ? (hedgeMv / total) * 100 : 0;
  const cashPct = total > 0 ? (cashMv / total) * 100 : 100;
  const maxTotalPct = account.risk_limits?.max_total_position_pct ?? null;
  const history = equityHistorySeries(account);

  return (
    <div className="mx-3 mt-3 grid grid-cols-1 gap-3 lg:grid-cols-[1.2fr_1fr_1fr]">
      <Card
        decoration="top"
        decorationColor={accountError ? "amber" : "cyan"}
        className="!bg-panel !ring-border h-full"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <Text className="uppercase tracking-wide">Net liquidation value</Text>
            <Metric className="mt-1 font-mono !text-3xl tabular-nums text-ink">{fmtMoney(account.portfolio_value)}</Metric>
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
            className="mt-2 h-9"
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

      <Card decoration="top" decorationColor="cyan" className="!bg-panel !ring-border h-full">
        <div className="flex items-start justify-between gap-3">
          <div>
            <Text className="uppercase tracking-wide">Portfolio exposure</Text>
            <Title className="mt-1 font-mono !text-2xl text-ink">{riskDeployedPct.toFixed(0)}% deployed</Title>
          </div>
          {maxTotalPct !== null && <Badge color="slate">ceiling {maxTotalPct.toFixed(0)}%</Badge>}
        </div>
        <ProgressBar value={riskDeployedPct} color="cyan" className="mt-3" />
        <CategoryBar
          values={[longPct, hedgePct, cashPct]}
          colors={["emerald", "fuchsia", "slate"]}
          showLabels={false}
          className="mt-4"
        />
        <Legend
          categories={[
            `Long ${fmtMoneyCompact(longMv)}`,
            `Hedge ${fmtMoneyCompact(hedgeMv)}`,
            `Cash ${fmtMoneyCompact(cashMv)}`,
          ]}
          colors={["emerald", "fuchsia", "slate"]}
          className="mt-2"
        />
      </Card>

      <RegimeCard funnel={funnel} />
    </div>
  );
}
