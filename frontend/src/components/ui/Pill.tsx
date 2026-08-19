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

  // risk_verdict.verdict.reason_category (src/models.py::RiskVerdict) — the
  // specific categorized "why" behind a modification/rejection. "clean" is
  // reused below for the derived clean/modified/rejected outcome label too.
  clean: "bg-pos/15 text-pos",
  oversized: "bg-warn/15 text-warn",
  rr_fail: "bg-neg/15 text-neg",
  concentration: "bg-warn/15 text-warn",
  correlation_risk: "bg-warn/15 text-warn",
  event_risk: "bg-warn/15 text-warn",
  macro_misalign: "bg-warn/15 text-warn",
  data_degraded: "bg-neg/15 text-neg",
  signal_fidelity: "bg-warn/15 text-warn",

  // Per-specialist alignment vs detail.consensus.agreement (SpecialistCards).
  diverges: "bg-warn/15 text-warn",
};

export function Pill({ text }: { text: string | null | undefined }) {
  const key = (text || "").toLowerCase().replace(/[^a-z_]/g, "");
  const cls = PILL_COLORS[key] || "bg-dim/15 text-dim";
  return <span className={`pill ${cls}`}>{(text || "—").toString().toUpperCase()}</span>;
}
