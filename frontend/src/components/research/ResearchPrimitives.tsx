import type { ReactNode } from "react";
import type { ResearchDirection, ResearchEvidenceItem, ResearchItemStatus } from "../../api/client";

export const SEAT_LABELS: Record<string, string> = {
  technical: "Technical", tech_analyst: "Technical", news: "News", news_analyst: "News",
  macro: "Macro", macro_analyst: "Macro", earnings: "Earnings", earnings_analyst: "Earnings",
  smart_money: "Smart Money", smart_money_analyst: "Smart Money", portfolio_manager: "Portfolio Manager",
  ai_risk: "AI Risk Manager", risk_manager: "AI Risk Manager", position_reviewer: "Position Reviewer",
  evening_review: "Evening Review", meta_reflection: "Meta-Reflection",
};

const SEAT_GLYPHS: Record<string, string> = {
  technical: "↗", news: "≋", macro: "◎", earnings: "§", smart_money: "◆",
  portfolio_manager: "◇", ai_risk: "■", position_reviewer: "✓",
  evening_review: "☾", meta_reflection: "↻",
};

export function seatLabel(seat: string) {
  return SEAT_LABELS[seat.toLowerCase()] || seat.replace(/_/g, " ");
}

export function SeatLabel({ seat }: { seat: string }) {
  const label = seatLabel(seat);
  return <span className="research-semantic-label"><span aria-hidden="true">{SEAT_GLYPHS[seat] || "•"}</span>{label}</span>;
}

export function SemanticLabel({ kind, children }: { kind: "change" | "tension" | "now" | "evidence"; children: ReactNode }) {
  const glyph = { change: "Δ", tension: "⚖", now: "⚡", evidence: "◈" }[kind];
  return <span className="research-semantic-label"><span aria-hidden="true">{glyph}</span>{children}</span>;
}

export function toneForDirection(direction: ResearchDirection) {
  if (direction === "bullish") return "text-pos border-pos/30 bg-pos/5";
  if (direction === "bearish") return "text-neg border-neg/30 bg-neg/5";
  if (direction === "mixed") return "text-warn border-warn/30 bg-warn/5";
  return "text-dim border-border bg-panel-inset";
}

export function DirectionBadge({ direction }: { direction: ResearchDirection }) {
  return <span className={`research-badge ${toneForDirection(direction)}`}>{direction}</span>;
}

export function StatusBadge({ status }: { status: ResearchItemStatus }) {
  const cls = status === "current" ? "text-pos border-pos/30" : status === "historical" ? "text-dim border-border" : status === "error" || status === "unavailable" ? "text-neg border-neg/30" : "text-warn border-warn/30";
  return <span className={`research-badge ${cls}`}>{status}</span>;
}

export function EvidenceStrip({ items }: { items: ResearchEvidenceItem[] }) {
  if (!items.length) return null;
  return <div className="evidence-strip">{items.map((item, index) => (
    <span className="evidence-chip" key={`${item.label}-${index}`} title={[item.source, item.timestamp].filter(Boolean).join(" · ")}>
      <span>{item.label}</span><strong>{item.value}</strong>
    </span>
  ))}</div>;
}

export function Eyebrow({ children, tone = "default" }: { children: ReactNode; tone?: "default" | "warn" | "agent" }) {
  return <div className={`research-eyebrow ${tone === "warn" ? "text-warn" : tone === "agent" ? "text-agent" : "text-dim"}`}>{children}</div>;
}

export function ResearchState({ status, errors = [] }: { status: ResearchItemStatus; errors?: string[] }) {
  const copy = status === "quiet" ? "No material research change was recorded for this day. Quiet is a valid read."
    : status === "historical" ? "Archived research for the selected day is shown below. Its timestamps are historical by design."
    : status === "aging" ? "The latest read is aging. Check the as-of times before treating it as current."
    : status === "stale" ? "Last-known research is shown below. Check the as-of times before treating it as current."
    : status === "partial" ? "The read is incomplete. Available evidence remains visible; missing coverage is named."
    : status === "error" ? "Research synthesis could not be loaded. Trading is unaffected."
    : status === "unavailable" ? "No stored research read is available for this date."
    : null;
  if (!copy && !errors.length) return null;
  return <div className={`research-state ${status === "historical" ? "border-border text-dim" : status === "error" ? "border-neg/40 text-neg" : "border-warn/40 text-warn"}`}>
    <strong>{copy}</strong>{errors.map((error, index) => <span key={index}>{error}</span>)}
  </div>;
}
