import { Badge, type Color } from "@tremor/react";

const PILL_COLORS: Record<string, Color> = {
  buy: "emerald",
  strong_buy: "emerald",
  bullish: "emerald",
  approved: "emerald",
  aligned: "emerald",
  filled: "emerald",
  executed: "emerald",
  long: "slate",
  sell: "rose",
  strong_sell: "rose",
  bearish: "rose",
  rejected: "rose",
  hard_risk_block: "rose",
  hold: "slate",
  neutral: "slate",
  unknown: "slate",
  no_candidates: "slate",
  insufficient_data: "slate",
  no_directional_signal: "slate",
  open: "cyan",
  proposed: "amber",
  modified: "amber",
  mixed: "amber",
  proposed_not_executed: "amber",
  no_proposal: "amber",
  bearish_hedge: "fuchsia",
  cash_equivalent: "amber",

  // risk_verdict.verdict.reason_category (src/models.py::RiskVerdict) — the
  // specific categorized "why" behind a modification/rejection. "clean" is
  // reused below for the derived clean/modified/rejected outcome label too.
  clean: "emerald",
  oversized: "amber",
  rr_fail: "rose",
  concentration: "amber",
  correlation_risk: "amber",
  event_risk: "amber",
  macro_misalign: "amber",
  data_degraded: "rose",
  signal_fidelity: "amber",

  // Per-specialist alignment vs detail.consensus.agreement (SpecialistCards).
  diverges: "amber",
};

export function Pill({ text }: { text: string | null | undefined }) {
  const key = (text || "").toLowerCase().replace(/[^a-z_]/g, "");
  return (
    <Badge color={PILL_COLORS[key] || "slate"} size="xs" className="font-semibold uppercase tracking-wide">
      {(text || "—").toString().replace(/_/g, " ")}
    </Badge>
  );
}
