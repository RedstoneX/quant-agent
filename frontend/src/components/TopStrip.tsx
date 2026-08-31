import { Badge, type Color } from "@tremor/react";
import { AccountResponse, HealthResponse } from "../api/client";

function healthColor(health: HealthResponse | null): { dot: string; label: string } {
  if (!health) return { dot: "bg-dim", label: "health unavailable" };
  if (!health.db_reachable) return { dot: "bg-neg", label: "database unreachable" };
  // Ranked directly under "the database is gone" and above everything
  // else on purpose: every other fault on this board is reported to the
  // operator over Telegram, so a dead alert channel hides all of them.
  // This banner is the only place a fully-dead channel can still say so.
  const channel = health.alert_channel;
  if (channel?.status === "broken") {
    return { dot: "bg-neg", label: "ALERT CHANNEL BROKEN — alarms reach nobody" };
  }
  if (channel?.status === "stale") {
    return { dot: "bg-neg", label: "alert channel unverified — no recent check" };
  }
  if (!channel || channel.status === "unknown") {
    return { dot: "bg-warn", label: "alert channel never verified" };
  }
  if (health.broker_reachable === false) return { dot: "bg-warn", label: "broker unreachable" };
  if (health.broker_reachable === null) return { dot: "bg-warn", label: "broker not configured" };
  const circuit = health.llm_circuit;
  if (circuit && !circuit.available) {
    return { dot: "bg-neg", label: "paid-analysis safety circuit unavailable" };
  }
  if (circuit?.requires_operator_reset) {
    return { dot: "bg-neg", label: "paid analysis suspended — operator reset required" };
  }
  if (circuit?.suspended && circuit.suspension_class === "quota") {
    return { dot: "bg-warn", label: "paid analysis held — auto-rearms next ET budget day" };
  }
  if ((circuit?.active_quota_holds?.length ?? 0) > 0) {
    return { dot: "bg-warn", label: "scoped paid-analysis quota hold — other sessions eligible" };
  }
  if (circuit?.recent_recovery) {
    return { dot: "bg-pos", label: "paid analysis rearmed — checks passed" };
  }
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
      ? { text: "PAPER", color: "emerald" as Color }
      : paper === false
      ? { text: "LIVE — REAL MONEY", color: "rose" as Color }
      : { text: "MODE UNKNOWN", color: "slate" as Color };
  const { dot, label } = healthColor(health);

  return (
    <header className="sticky top-0 z-10 bg-bg border-b border-border flex items-center gap-5 flex-wrap px-4 py-2">
      <div className="flex items-center gap-2 flex-shrink-0">
        <h1 className="text-[0.95rem] font-bold whitespace-nowrap m-0">
          QAMC <span className="text-dim font-normal">Mission Control</span>
        </h1>
        <Badge color={modeBadge.color} size="sm">{modeBadge.text}</Badge>
        <span className={`inline-block w-2.5 h-2.5 rounded-full ${dot}`} title={label} />
      </div>

      {accountError && (
        <div className={`text-[0.875rem] ${account ? "text-warn" : "text-neg"}`}>
          {account ? `Account: showing last known data — ${accountError}` : `Account unavailable: ${accountError}`}
        </div>
      )}

      <div className="ml-auto flex flex-wrap items-center gap-3 text-[0.8125rem] text-dim">
        <span>{label}</span>
        {updatedAt && <span>updated {updatedAt.toLocaleTimeString()}</span>}
        <a href="/ui/" className="underline hover:text-accent" title="Original Stage 3-5 dashboard">
          legacy view
        </a>
      </div>
    </header>
  );
}
