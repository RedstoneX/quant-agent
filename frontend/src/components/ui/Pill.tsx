const PILL_COLORS: Record<string, string> = {
  buy: "bg-pos/15 text-pos",
  strong_buy: "bg-pos/15 text-pos",
  bullish: "bg-pos/15 text-pos",
  approved: "bg-pos/15 text-pos",
  aligned: "bg-pos/15 text-pos",
  filled: "bg-pos/15 text-pos",
  executed: "bg-pos/15 text-pos",
  long: "bg-dim/15 text-dim",
  sell: "bg-neg/15 text-neg",
  strong_sell: "bg-neg/15 text-neg",
  bearish: "bg-neg/15 text-neg",
  rejected: "bg-neg/15 text-neg",
  hard_risk_block: "bg-neg/15 text-neg",
  hold: "bg-dim/15 text-dim",
  neutral: "bg-dim/15 text-dim",
  unknown: "bg-dim/15 text-dim",
  no_candidates: "bg-dim/15 text-dim",
  insufficient_data: "bg-dim/15 text-dim",
  no_directional_signal: "bg-dim/15 text-dim",
  open: "bg-accent/15 text-accent",
  proposed: "bg-warn/15 text-warn",
  modified: "bg-warn/15 text-warn",
  mixed: "bg-warn/15 text-warn",
  proposed_not_executed: "bg-warn/15 text-warn",
  no_proposal: "bg-warn/15 text-warn",
  bearish_hedge: "bg-hedge/15 text-hedge",
  cash_equivalent: "bg-warn/15 text-warn",
};

export function Pill({ text }: { text: string | null | undefined }) {
  const key = (text || "").toLowerCase().replace(/[^a-z_]/g, "");
  const cls = PILL_COLORS[key] || "bg-dim/15 text-dim";
  return <span className={`pill ${cls}`}>{(text || "—").toString().toUpperCase()}</span>;
}
