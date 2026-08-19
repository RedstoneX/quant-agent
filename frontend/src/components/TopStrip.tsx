import { AccountResponse, HealthResponse } from "../api/client";

function healthColor(health: HealthResponse | null): { dot: string; label: string } {
  if (!health) return { dot: "bg-dim", label: "health unavailable" };
  if (!health.db_reachable) return { dot: "bg-neg", label: "database unreachable" };
  if (health.broker_reachable === false) return { dot: "bg-warn", label: "broker unreachable" };
  if (health.broker_reachable === null) return { dot: "bg-warn", label: "broker not configured" };
  return { dot: "bg-pos", label: "all systems reachable" };
}

// Slim app-chrome header — brand, PAPER/LIVE mode, system health, legacy
// link. The actual account KPIs (equity/P&L/exposure/regime) live in
// `HeroBand` below this, which owns the "what do I own, what's the market
// doing" first-glance job; this bar is identity/status only.
export function TopStrip({
  account,
  accountError,
  health,
  updatedAt,
}: {
  account: AccountResponse | null;
  accountError: string | null;
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

  return (
    <header className="sticky top-0 z-10 bg-bg border-b border-border flex items-center gap-5 flex-wrap px-4 py-2">
      <div className="flex items-center gap-2 flex-shrink-0">
        <h1 className="text-[0.95rem] font-bold whitespace-nowrap m-0">
          QAMC <span className="text-dim font-normal">Mission Control</span>
        </h1>
        <span className={`pill border ${modeBadge.cls}`}>{modeBadge.text}</span>
        <span className={`inline-block w-2.5 h-2.5 rounded-full ${dot}`} title={label} />
      </div>

      {accountError && (
        <div className={`text-[0.82rem] ${account ? "text-warn" : "text-neg"}`}>
          {account ? `Account: showing last known data — ${accountError}` : `Account unavailable: ${accountError}`}
        </div>
      )}

      <div className="ml-auto flex items-center gap-3 flex-shrink-0 text-[0.72rem] text-dim">
        <span>{label}</span>
        {updatedAt && <span>updated {updatedAt.toLocaleTimeString()}</span>}
        <a href="/ui/" className="underline hover:text-accent" title="Original Stage 3-5 dashboard">
          legacy view
        </a>
      </div>
    </header>
  );
}
