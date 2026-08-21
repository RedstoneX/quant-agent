import { ReactNode } from "react";
import { Badge, Card, type Color } from "@tremor/react";

export type PanelStatus = "ok" | "degraded" | "error" | "loading" | "stale";

export function Panel({
  title,
  subtitle,
  status,
  staleSince,
  actions,
  full,
  accent,
  children,
}: {
  title: string;
  subtitle?: string;
  status?: PanelStatus;
  /** The last-known-good fetch time. Rendered next to the status word
   * whenever known — not only when `status === "stale"` — so a value on
   * screen is never an unqualified "this is current right now" claim;
   * see docs/architecture/MISSION_CONTROL_API.md "Mission Control
   * data-truth" tranche ("source it explicitly and timestamp it"). */
  staleSince?: Date | null;
  actions?: ReactNode;
  full?: boolean;
  accent?: boolean;
  children: ReactNode;
}) {
  const statusColor: Color =
    status === "error" ? "rose" : status === "stale" || status === "degraded" ? "amber" : status === "ok" ? "emerald" : "slate";
  const statusLabel =
    status === "loading"
      ? "…"
      : status === "stale"
      ? `stale${staleSince ? ` · ${staleSince.toLocaleTimeString()}` : ""}`
      : status
      ? `${status}${staleSince ? ` · ${staleSince.toLocaleTimeString()}` : ""}`
      : "";
  return (
    <Card
      decoration={accent ? "top" : undefined}
      decorationColor={accent ? "cyan" : undefined}
      className={`!p-0 !bg-panel !ring-border rounded-xl overflow-hidden flex flex-col h-full ${full ? "md:col-span-2" : ""} ${
        status === "stale" ? "!ring-warn/60" : ""
      }`}
    >
      <div className="panel-head">
        <h2>{title}</h2>
        <div className="ml-auto flex items-center gap-2">
          {actions}
          {statusLabel && <Badge color={statusColor} size="xs">{statusLabel}</Badge>}
        </div>
        {subtitle && <span className="basis-full text-[0.8125rem] text-dim">{subtitle}</span>}
      </div>
      <div className="panel-body">{children}</div>
    </Card>
  );
}

export function StateMessage({
  text,
  error,
  hero,
  glyph,
}: {
  text: string;
  error?: boolean;
  hero?: boolean;
  /** Fix 3 (visual convergence plan §2.4): a single large geometric glyph
   * reusing DecisionStateBanner's existing ●/◐/■/○ vocabulary — never a new
   * icon system — rendered above the message in the `hero` variant. Turns
   * "three lines of dim centered text in a void" into a composed
   * placeholder. Optional: the "Loading…" hero calls intentionally pass no
   * glyph, since a static geometric mark on a transient state reads as a
   * stuck icon rather than progress. */
  glyph?: string;
}) {
  if (hero) {
    // Used when this message is the ONLY content in a panel. When that
    // panel's column is viewport-locked (App.tsx's populated-day
    // xl:h-[calc(100vh-var(--chrome-h))]), `h-full` fills it and this
    // block centers within the tall column exactly as before; when the
    // column is instead collapsed to content height (App.tsx's Fix 3
    // no-session state), `h-full` resolves to auto against an auto-height
    // ancestor and `min-h-[220px]` becomes the effective height — a small,
    // content-sized card instead of a void stretched to match a viewport
    // calculation that no longer means anything.
    return (
      <div className="h-full min-h-[220px] flex flex-col items-center justify-center text-center px-6 gap-2.5">
        {glyph && (
          <span className={`text-[length:var(--glyph-hero)] leading-none ${error ? "text-neg" : "text-faint"}`} aria-hidden="true">
            {glyph}
          </span>
        )}
        <p className={`text-[0.875rem] leading-relaxed max-w-[26ch] ${error ? "text-neg" : "text-dim"}`}>{text}</p>
      </div>
    );
  }
  return <div className={`state-message ${error ? "text-neg" : ""}`}>{text}</div>;
}
