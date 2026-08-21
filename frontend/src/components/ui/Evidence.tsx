import { ReactNode } from "react";

export function Card({
  title,
  broader,
  children,
}: {
  title: string;
  broader?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`card ${broader ? "border-dashed border-accent bg-accent/5" : ""}`}>
      <div className="flex items-center justify-between gap-2 mb-1.5 flex-wrap">
        <span className="font-bold text-[0.95rem]">{title}</span>
        {broader && (
          <span className="pill border border-accent/40 bg-accent/15 text-accent">Market-wide</span>
        )}
      </div>
      {children}
    </div>
  );
}

export function KV({ label, value }: { label: string; value: ReactNode }) {
  const dash = value === null || value === undefined || value === "" ? "—" : value;
  return (
    <div className="kv-row">
      <span className="text-dim flex-shrink-0">{label}</span>
      <span className="text-right tabular-nums break-words">{dash}</span>
    </div>
  );
}

export function CardText({ text, dim }: { text: string; dim?: boolean }) {
  return <p className={`text-[0.875rem] mt-1.5 leading-snug ${dim ? "text-dim" : ""}`}>{text}</p>;
}

export function EvidenceSection({
  title,
  emptyText = "Not available for this candidate/run.",
  children,
}: {
  title: string;
  emptyText?: string;
  children: ReactNode[];
}) {
  const body = children.filter((c) => c !== null && c !== undefined && c !== false);
  return (
    <div className="mb-5 last:mb-0">
      <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-2 pb-1 border-b border-border">
        {title}
      </div>
      {body.length ? <div className="flex flex-col gap-3">{body}</div> : <div className="state-message">{emptyText}</div>}
    </div>
  );
}
