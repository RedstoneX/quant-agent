/* Small reusable visual primitives so confidence/exposure/funnel data reads
 * as a meter or bar instead of another row of numbers — used by
 * SpecialistCards (conviction), LiquidityPanel (cash/exposure buckets),
 * and CandidateRail (funnel). Every value rendered here comes from the
 * caller's own real data; these components never invent a number. */

import { ProgressBar, Tracker, type Color } from "@tremor/react";

const TONE_BAR: Record<string, Color> = {
  pos: "emerald",
  neg: "rose",
  warn: "amber",
  accent: "cyan",
  hedge: "fuchsia",
  dim: "slate",
};

export function Meter({ value, tone = "accent", label }: { value: number; tone?: keyof typeof TONE_BAR; label?: string }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className="w-full">
      {label && <div className="text-[0.7rem] text-dim uppercase tracking-wide mb-0.5">{label}</div>}
      <ProgressBar value={pct} color={TONE_BAR[tone]} className="mt-1" />
    </div>
  );
}

const LEVEL_RANK: Record<string, number> = { low: 1, medium: 2, high: 3 };

// A discrete 3-segment level indicator for qualitative low/medium/high
// fields (specialist conviction, macro-regime confidence) that the backend
// never expresses as a number. Deliberately NOT a continuous percentage
// fill: mapping "high"/"medium"/"low" to an invented width like 92%/58%/
// 28% would imply a measured precision the model never produced — exactly
// what docs/OUTCOME.md's agent-card principle warns against ("do not
// invent pseudo-confidence percentages... show the actual qualitative
// state rather than manufacturing precision"). Filled segment count (1/2/3)
// still reads as "more/less" at a glance without claiming a number.
export function LevelBar({ level, tone = "accent" }: { level: string | null | undefined; tone?: keyof typeof TONE_BAR }) {
  const rank = level ? LEVEL_RANK[level.toLowerCase()] ?? 0 : 0;
  return (
    <Tracker
      className="!h-1.5"
      aria-label={level ? `${level} level` : "level unknown"}
      data={[1, 2, 3].map((i) => ({ color: i <= rank ? TONE_BAR[tone] : "slate", tooltip: i <= rank ? level || undefined : "not reached" }))}
    />
  );
}
