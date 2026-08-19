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
          <span className={`text-[0.68rem] font-semibold uppercase tracking-wide whitespace-nowrap ${statusColor}`}>
            {statusLabel}
          </span>
        </div>
        {subtitle && <span className="basis-full text-[0.72rem] text-dim">{subtitle}</span>}
      </div>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function StateMessage({ text, error }: { text: string; error?: boolean }) {
  return <div className={`state-message ${error ? "text-neg" : ""}`}>{text}</div>;
}
