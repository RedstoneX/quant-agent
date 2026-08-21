import { ReactNode } from "react";

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
  /** When `status === "stale"`, the last-known-good fetch time — rendered
   * next to the status word so "stale" is never a bare unqualified claim. */
  staleSince?: Date | null;
  actions?: ReactNode;
  full?: boolean;
  accent?: boolean;
  children: ReactNode;
}) {
  const statusColor =
    status === "error" ? "text-neg" : status === "stale" || status === "degraded" ? "text-warn" : status === "ok" ? "text-pos" : "text-dim";
  const statusLabel =
    status === "loading" ? "…" : status === "stale" ? `stale${staleSince ? ` · ${staleSince.toLocaleTimeString()}` : ""}` : status || "";
  return (
    <section
      className={`panel ${full ? "md:col-span-2" : ""} ${
        accent ? "border-accent/40 shadow-[0_10px_28px_-12px_rgb(var(--c-accent)/0.4)]" : ""
      } ${status === "stale" ? "border-warn/50" : ""}`}
    >
      <div className="panel-head">
        <h2>{title}</h2>
        <div className="ml-auto flex items-center gap-2">
          {actions}
          <span className={`text-[0.75rem] font-semibold uppercase tracking-wide whitespace-nowrap ${statusColor}`}>
            {statusLabel}
          </span>
        </div>
        {subtitle && <span className="basis-full text-[0.8125rem] text-dim">{subtitle}</span>}
      </div>
      <div className="panel-body">{children}</div>
    </section>
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
