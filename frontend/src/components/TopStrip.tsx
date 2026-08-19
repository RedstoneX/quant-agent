import { AccountResponse, HealthResponse, PositionItem } from "../api/client";
import { fmtMoney, fmtMoneyCompact, fmtPct, pnlClass } from "../lib/format";

function Stat({ label, value, cls = "" }: { label: string; value: string; cls?: string }) {
  return (
    <div className="px-3.5 py-0.5 border-r border-border last:border-r-0 whitespace-nowrap">
      <div className="text-[0.62rem] text-dim uppercase tracking-wide">{label}</div>
      <div className={`text-[1rem] font-bold tabular-nums ${cls}`}>{value}</div>
    </div>
  );
}

function healthColor(health: HealthResponse | null): { dot: string; label: string } {
  if (!health) return { dot: "bg-dim", label: "health unavailable" };
  if (!health.db_reachable) return { dot: "bg-neg", label: "database unreachable" };
  if (health.broker_reachable === false) return { dot: "bg-warn", label: "broker unreachable" };
  if (health.broker_reachable === null) return { dot: "bg-warn", label: "broker not configured" };
  return { dot: "bg-pos", label: "all systems reachable" };
}

export function TopStrip({
  account,
  positions,
  health,
  updatedAt,
}: {
  account: AccountResponse | null;
  positions: PositionItem[];
  health: HealthResponse | null;
  updatedAt: Date | null;
}) {
  const paper = account?.paper;
  const modeBadge =
    paper === true
      ? { text: "PAPER", cls: "bg-pos/15 text-pos border-pos/40" }
      : paper === false
      ? { text: "LIVE — REAL MONEY", cls: "bg-neg/15 text-neg border-neg/40" }
      : { text: "MODE UNKNOWN", cls: "bg-dim/15 text-dim border-border" };
  const { dot, label } = healthColor(health);

  const unrealized = positions
    .filter((p) => !p.is_cash_equivalent)
    .reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
  const liq = account?.liquidity;

  return (
    <header className="sticky top-0 z-10 bg-bg border-b border-border flex items-center gap-5 flex-wrap px-4 py-2">
      <div className="flex items-center gap-2 flex-shrink-0">
        <h1 className="text-[0.95rem] font-bold whitespace-nowrap m-0">
          QAMC <span className="text-dim font-normal">Mission Control</span>
        </h1>
        <span className={`pill border ${modeBadge.cls}`}>{modeBadge.text}</span>
        <span className={`inline-block w-2.5 h-2.5 rounded-full ${dot}`} title={label} />
      </div>

      {account?.error ? (
        <div className="text-neg text-[0.82rem]">Account unavailable: {account.error}</div>
      ) : (
        // flex-wrap + a real min-width, not overflow-x-auto: at tablet
        // widths this strip doesn't have room for all 5 stats plus the
        // brand/meta blocks on one line, and a horizontally-scrolling
        // strip hid every stat but the first behind an undiscoverable
        // scroll. min-w-[320px] forces the *strip* to wrap onto its own
        // row instead of being squeezed to near-zero width; flex-wrap
        // then lets its own stat items wrap across 1-2 lines there.
        <div className="flex items-stretch flex-wrap flex-1 min-w-[320px] border-l border-r border-border">
          <Stat label="Equity" value={fmtMoney(account?.portfolio_value)} />
          <Stat
            label="Day P&L"
            value={`${fmtMoney(account?.daily_pnl)} (${fmtPct(account?.daily_pnl_pct)})`}
            cls={pnlClass(account?.daily_pnl)}
          />
          <Stat label="Unrealized P&L" value={fmtMoney(unrealized)} cls={pnlClass(unrealized)} />
          <Stat label="Deployable cash" value={fmtMoneyCompact(liq?.deployable_cash)} />
          <Stat
            label="Sweep parked"
            value={liq?.sweep_enabled ? `${fmtMoneyCompact(liq.sweep_parked_value)} ${liq.sweep_symbol || ""}` : "disabled"}
          />
        </div>
      )}

      <div className="flex items-center gap-3 flex-shrink-0 text-[0.72rem] text-dim">
        <span>{label}</span>
        {updatedAt && <span>updated {updatedAt.toLocaleTimeString()}</span>}
        <a href="/ui/" className="underline hover:text-accent" title="Original Stage 3-5 dashboard">
          legacy view
        </a>
      </div>
    </header>
  );
}
