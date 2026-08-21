import { SparkAreaChart } from "@tremor/react";
import { AccountResponse, PositionItem, RunFunnelResponse } from "../api/client";
import { fmtMoney, fmtMoneyCompact, fmtPct, pnlClass } from "../lib/format";
import { ArcGauge, GaugeBand } from "./ui/ArcGauge";
import { Pill } from "./ui/Pill";
import { LevelBar } from "./ui/Meter";

/* The cockpit's first-glance surface — WORK.md's questions 1 ("what do I
 * own / how much real risk is deployed") and 2 ("what does QAMC think the
 * market is doing") answered before any interaction, per the operator-
 * approved Concept C hero-band composition. Three blocks: equity/P&L +
 * sparkline, a real segmented-color-band risk gauge (ECharts) with a
 * long/hedge/cash legend, and the latest run's market-regime read. */

function equityHistorySeries(account: AccountResponse | null): { date: string; equity: number }[] {
  if (!account?.history?.length) return [];
  // API returns history newest-first; charts read left-to-right chronological.
  return [...account.history]
    .reverse()
    .map((p) => ({ date: p.date, equity: p.equity_close ?? p.total_value ?? 0 }))
    .filter((p) => p.equity > 0);
}

function RegimeBadge({ funnel }: { funnel: RunFunnelResponse | null }) {
  const macro = funnel?.macro_context;
  if (!macro || !macro.regime) {
    return (
      <div className="flex flex-col gap-1">
        <div className="text-[0.62rem] text-dim uppercase tracking-wide font-semibold">Market regime</div>
        <div className="text-dim text-[0.8rem]">No macro regime evidence for the latest run.</div>
      </div>
    );
  }
  const outlookTone = macro.equity_outlook === "bullish" ? "text-pos" : macro.equity_outlook === "bearish" ? "text-neg" : "text-dim";
  return (
    <div className="flex flex-col gap-1.5 h-full">
      <div className="text-[0.62rem] text-dim uppercase tracking-wide font-semibold">Market regime</div>
      <div className="flex items-center gap-2 flex-wrap">
        <Pill text={macro.regime} />
        <span className={`text-[0.82rem] font-bold ${outlookTone}`}>{(macro.equity_outlook || "unknown").toUpperCase()}</span>
      </div>
      {macro.confidence && (
        <div>
          <div className="flex items-center justify-between text-[0.66rem] text-dim mb-0.5">
            <span>Confidence</span>
            <span className="font-semibold text-ink">{macro.confidence.toUpperCase()}</span>
          </div>
          {/* Discrete 3-segment level, not a fabricated percentage fill —
              the macro agent only ever reports high/medium/low, and a
              precise-looking width (e.g. "58%") would claim a measurement
              that was never made. See docs/OUTCOME.md's agent-card
              principle / ui/Meter.tsx's LevelBar. */}
          <LevelBar level={macro.confidence} tone="accent" />
        </div>
      )}
      {macro.summary && <p className="text-[0.75rem] text-dim leading-snug line-clamp-2">{macro.summary}</p>}
    </div>
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
      <div className="mx-3 mt-3 rounded-xl border border-border bg-panel px-4 py-6 text-center text-dim text-[0.85rem]">
        {accountError ? `Account unavailable: ${accountError}` : "Loading account…"}
      </div>
    );
  }

  const unrealized = positions.filter((p) => !p.is_cash_equivalent).reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
  const liq = account.liquidity;
  const total = account.portfolio_value || 0;
  const longMv = positions.filter((p) => p.direction === "long").reduce((s, p) => s + (p.market_value || 0), 0);
  const hedgeMv = positions.filter((p) => p.direction === "bearish_hedge").reduce((s, p) => s + (p.market_value || 0), 0);
  const cashMv = Math.max(total - longMv - hedgeMv, 0);
  const riskDeployedPct = total > 0 ? ((longMv + hedgeMv) / total) * 100 : 0;
  // Gauge bands scaled against the deterministic risk gate's OWN
  // configured hard-block ceiling (config/settings.yaml's
  // risk.max_total_position_pct — src/risk/rules.py's actual limit, never
  // recomputed here) rather than an arbitrary UI-only split. Genuinely
  // informational only: this display never feeds back into risk/execution
  // eligibility. Falls back to a single neutral tone (no red/amber/green
  // judgment at all) when the limit isn't available, rather than reverting
  // to a made-up threshold — see docs/WORK.md's "arbitrary UI risk
  // thresholds must be removed or tied to authoritative data".
  const maxTotalPct = account.risk_limits?.max_total_position_pct ?? null;
  const gaugeBands: GaugeBand[] =
    maxTotalPct !== null && maxTotalPct > 0
      ? [
          { upTo: Math.min(maxTotalPct * 0.67, 100), tone: "pos" },
          { upTo: Math.min(maxTotalPct, 100), tone: "warn" },
          { upTo: 100, tone: "neg" },
        ]
      : [{ upTo: 100, tone: "accent" }];
  const history = equityHistorySeries(account);
  const stale = Boolean(accountError);

  return (
    <div
      className={`mx-3 mt-3 rounded-xl border bg-panel px-4 py-3.5 grid grid-cols-1 lg:grid-cols-[1.3fr_1fr_1fr] gap-4 ${
        stale ? "border-warn/50" : "border-border"
      }`}
    >
      {/* Equity / P&L */}
      <div className="flex flex-col gap-1 min-w-0">
        <div className="text-[0.62rem] text-dim uppercase tracking-wide font-semibold">Equity</div>
        <div className="hero-num">{fmtMoney(account.portfolio_value)}</div>
        <div className="flex items-center gap-3 flex-wrap text-[0.8rem]">
          <span className={`font-mono num font-semibold ${pnlClass(account.daily_pnl)}`}>
            {fmtMoney(account.daily_pnl)} ({fmtPct(account.daily_pnl_pct)}) today
          </span>
          <span className={`font-mono num ${pnlClass(unrealized)}`}>{fmtMoney(unrealized)} unrealized</span>
        </div>
        {history.length > 1 && (
          <div className="mt-1">
            <SparkAreaChart
              data={history}
              index="date"
              categories={["equity"]}
              colors={[pnlClass(account.daily_pnl).includes("neg") ? "rose" : "emerald"]}
              className="h-10 w-full"
              showGradient
            />
          </div>
        )}
        <div className="flex items-center gap-3 flex-wrap text-[0.72rem] text-dim mt-0.5">
          <span>
            Deployable <span className="text-ink font-semibold font-mono num">{fmtMoneyCompact(liq?.deployable_cash)}</span>
          </span>
          <span>
            Sweep parked{" "}
            <span className="text-ink font-semibold font-mono num">
              {liq?.sweep_enabled ? `${fmtMoneyCompact(liq.sweep_parked_value)} ${liq.sweep_symbol || ""}` : "disabled"}
            </span>
          </span>
        </div>
      </div>

      {/* Risk exposure gauge */}
      <div className="flex flex-col items-center border-l border-border/70 pl-4 lg:pl-4 -ml-1">
        <ArcGauge
          value={riskDeployedPct}
          label="Risk deployed"
          valueLabel={`${riskDeployedPct.toFixed(0)}%`}
          bands={gaugeBands}
          height={112}
        />
        {maxTotalPct !== null && (
          <div className="text-[0.62rem] text-dim mt-0.5">vs. {maxTotalPct.toFixed(0)}% deterministic ceiling</div>
        )}
        <div className="flex items-center gap-3 flex-wrap justify-center text-[0.68rem] mt-1.5">
          <span className="inline-flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-pos" />
            <span className="text-dim">Long</span>
            <span className="font-mono num font-semibold">{fmtMoneyCompact(longMv)}</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-hedge" />
            <span className="text-dim">Hedge</span>
            <span className="font-mono num font-semibold">{fmtMoneyCompact(hedgeMv)}</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-faint" />
            <span className="text-dim">Cash</span>
            <span className="font-mono num font-semibold">{fmtMoneyCompact(cashMv)}</span>
          </span>
        </div>
      </div>

      {/* Regime */}
      <div className="border-l border-border/70 pl-4 lg:pl-4">
        <RegimeBadge funnel={funnel} />
      </div>
    </div>
  );
}
