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
import { AccountResponse, MacroBroaderContext, PositionItem } from "../api/client";
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

/** "as of HH:MM" for a regime reading's age — naive timestamps (no
 * trailing Z/offset) are UTC, same convention lib/format.ts::fmtTime
 * already uses everywhere else a run timestamp is displayed. */
function regimeAge(asOf: string | null): string | null {
  if (!asOf) return null;
  const d = new Date(asOf.endsWith("Z") || asOf.includes("+") ? asOf : `${asOf}Z`);
  if (isNaN(d.getTime())) return null;
  return `as of ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

/* Item 7 (cockpit trader rework): "Market Regime" used to be a permanent
 * empty state ("Awaiting macro evidence") whenever funnel-of-the-moment
 * carried no macro context — including the common case of a midday/close
 * position-review run with a perfectly good morning regime reading a few
 * hours old. App.tsx's `latestRegime` now looks across all of today's
 * already-fetched runs for the most recent real regime and passes it
 * here, with the reading's own age — so this shows the last known regime
 * WITH its age, and renders nothing at all (reclaiming the space rather
 * than reserving a placeholder) only on a day with truly no regime
 * evidence yet. */
function RegimeCard({ regime }: { regime: { macro: MacroBroaderContext; asOf: string | null } | null }) {
  if (!regime?.macro.regime) return null;
  const macro = regime.macro;
  const age = regimeAge(regime.asOf);

  const outlookTone =
    macro.equity_outlook === "bullish" ? "text-pos" : macro.equity_outlook === "bearish" ? "text-neg" : "text-dim";
  const confidenceTone = macro.confidence === "high" ? "pos" : macro.confidence === "medium" ? "warn" : "dim";

  return (
    <Card decoration="top" decorationColor="violet" className="!bg-panel !p-3.5 !ring-border h-full">
      <div className="flex items-start justify-between gap-2">
        <Text className="uppercase tracking-wide">Market regime</Text>
        {age && <span className="text-[length:var(--fs-micro)] text-dim whitespace-nowrap">{age}</span>}
      </div>
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
  regime,
  collapsed = false,
}: {
  account: AccountResponse | null;
  accountError: string | null;
  positions: PositionItem[];
  /** Last known regime reading across today's runs, with its age — see
   * App.tsx's `latestRegime`. Null on a day with no regime evidence yet. */
  regime: { macro: MacroBroaderContext; asOf: string | null } | null;
  /* Item 6 (cockpit trader rework): this is now the SECONDARY, compact
   * portfolio-abstractions band — Holdings (App.tsx's HoldingsStrip) leads
   * the header instead. Collapsed is the default; nothing here is
   * unreachable when collapsed, the same facts are still shown, just
   * denser, and "Show full header" switches back to the full cards. */
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
  // longMv / hedgeMv are DISPLAY sums only — a breakdown of where the money
  // sits, using `direction` for exactly the labeling it is documented for.
  const longMv = positions.filter((p) => p.direction === "long").reduce((sum, p) => sum + (p.market_value || 0), 0);
  const hedgeMv = positions
    .filter((p) => p.direction === "bearish_hedge")
    .reduce((sum, p) => sum + (p.market_value || 0), 0);
  const cashMv = Math.max(total - longMv - hedgeMv, 0);
  // "% deployed" comes from the SERVER, computed by the same function the
  // risk engine's max_total_position_pct rule uses. This used to be
  // (longMv + hedgeMv) / total — hedges ADDED instead of netted, leverage
  // ignored — and was then drawn against the engine's ceiling fetched from
  // /account, so the bar and its fill came from different definitions and
  // read 13.6 percentage points apart. Null means "not measurable", which
  // is rendered as such rather than as a confident 0%.
  const riskDeployedPct = account.exposure?.net_exposure_pct ?? null;
  const deployedLabel = riskDeployedPct === null ? "—" : `${riskDeployedPct.toFixed(0)}%`;
  const maxTotalPct = account.risk_limits?.max_total_position_pct ?? null;
  const history = equityHistorySeries(account);

  if (collapsed) {
    return (
      <div className="mx-3 mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border bg-panel px-3 py-2">
        <span className="flex items-baseline gap-2">
          <span className="label-xs">NLV</span>
          <span className="font-mono text-[length:var(--fs-stat)] font-semibold tabular-nums text-ink">
            {fmtMoney(account.portfolio_value)}
          </span>
        </span>
        {/* Item 11: the equity curve now lives here too, not only behind
            "Show full header" — this collapsed line is what a trader sees
            by default, so "+$X today and nothing else" needed fixing in
            the state that's actually on screen most of the time. */}
        {history.length > 1 && (
          <SparkAreaChart
            data={history}
            index="date"
            categories={["equity"]}
            colors={["cyan"]}
            className="h-6 w-20"
            showGradient
          />
        )}
        <span className={`font-mono text-[length:var(--fs-body)] font-semibold tabular-nums ${pnlClass(account.daily_pnl)}`}>
          {fmtMoney(account.daily_pnl)} ({fmtPct(account.daily_pnl_pct)}) today
        </span>
        <span className={`font-mono text-[length:var(--fs-body)] tabular-nums ${pnlClass(unrealized)}`}>
          {fmtMoney(unrealized)} unrealized
        </span>
        <span className="text-[length:var(--fs-meta)] text-dim">
          {deployedLabel} net exposure
          {maxTotalPct !== null ? ` / ${maxTotalPct.toFixed(0)}% ceiling` : ""}
        </span>
        {regime?.macro.regime && (
          <span className="flex items-center gap-1.5">
            <Pill text={regime.macro.regime} />
            <span
              className={`text-[length:var(--fs-meta)] font-bold tracking-wide ${
                regime.macro.equity_outlook === "bullish" ? "text-pos" : regime.macro.equity_outlook === "bearish" ? "text-neg" : "text-dim"
              }`}
            >
              {(regime.macro.equity_outlook || "unknown").toUpperCase()}
            </span>
            {regimeAge(regime.asOf) && <span className="text-[length:var(--fs-micro)] text-dim">{regimeAge(regime.asOf)}</span>}
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
    <div className={`mx-3 mt-3 grid grid-cols-1 gap-3 ${regime?.macro.regime ? "lg:grid-cols-[1.2fr_1fr_1fr]" : "lg:grid-cols-[1.2fr_1fr]"}`}>
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
            Deployable{" "}
            <strong
              className="font-mono text-ink"
              title="Cash plus the sweep vehicle the engine liquidates on demand — the figure position sizing actually uses."
            >
              {fmtMoneyCompact(liquidity?.deployable_cash)}
            </strong>
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
            <Title
              className="mt-1 font-mono !text-2xl text-ink"
              title="Net exposure: holdings after hedges are netted off, leveraged funds at their true multiple. Same measure as the ceiling beside it."
            >
              {deployedLabel} net exposure
            </Title>
          </div>
          {maxTotalPct !== null && <Badge color="slate">ceiling {maxTotalPct.toFixed(0)}%</Badge>}
        </div>
        <ProgressBar value={riskDeployedPct ?? 0} color="cyan" className="mt-3" />
        <Grid numItems={3} className="mt-3 gap-2">
          <div><Text className="text-xs uppercase">Long</Text><Metric className="font-mono text-base text-pos">{fmtMoneyCompact(longMv)}</Metric></div>
          <div><Text className="text-xs uppercase">Hedge</Text><Metric className="font-mono text-base text-hedge">{fmtMoneyCompact(hedgeMv)}</Metric></div>
          <div><Text className="text-xs uppercase">Liquidity</Text><Metric className="font-mono text-base text-ink">{fmtMoneyCompact(cashMv)}</Metric></div>
        </Grid>
      </Card>

      <RegimeCard regime={regime} />
    </div>
  );
}
