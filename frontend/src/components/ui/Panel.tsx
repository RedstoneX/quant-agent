import { ReactNode } from "react";

export function Panel({
  title,
  subtitle,
  status,
  actions,
  full,
  accent,
  children,
}: {
  title: string;
  subtitle?: string;
  status?: "ok" | "degraded" | "error" | "loading";
  actions?: ReactNode;
  full?: boolean;
  accent?: boolean;
  children: ReactNode;
}) {
  const statusColor =
    status === "error" ? "text-neg" : status === "degraded" ? "text-warn" : status === "ok" ? "text-pos" : "text-dim";
  return (
    <section className={`panel ${full ? "md:col-span-2" : ""} ${accent ? "border-accent/40" : ""}`}>
      <div className="panel-head">
        <h2>{title}</h2>
        <div className="ml-auto flex items-center gap-2">
          {actions}
          <span className={`text-[0.68rem] font-semibold uppercase tracking-wide ${statusColor}`}>
            {status === "loading" ? "…" : status || ""}
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
