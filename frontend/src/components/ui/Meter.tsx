/* Small reusable visual primitives so confidence/exposure/funnel data reads
 * as a meter or bar instead of another row of numbers — used by
 * SpecialistCards (conviction), LiquidityPanel (cash/exposure buckets),
 * and CandidateRail (funnel). Every value rendered here comes from the
 * caller's own real data; these components never invent a number. */

const TONE_BAR: Record<string, string> = {
  pos: "bg-pos",
  neg: "bg-neg",
  warn: "bg-warn",
  accent: "bg-accent",
  hedge: "bg-hedge",
  dim: "bg-dim",
};

export function Meter({ value, tone = "accent", label }: { value: number; tone?: keyof typeof TONE_BAR; label?: string }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className="w-full">
      {label && <div className="text-[0.6rem] text-dim uppercase tracking-wide mb-0.5">{label}</div>}
      <div className="h-1.5 w-full rounded-full bg-panel-alt overflow-hidden border border-border/60">
        <div className={`h-full rounded-full ${TONE_BAR[tone]}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export interface Segment {
  label: string;
  value: number;
  tone: keyof typeof TONE_BAR;
}

// A single proportional stacked bar (e.g. "where is the cash") plus a
// legend row with the real value under each segment — the same shape as
// DirectionalBiasPanel's DirectionRatioBar, generalized for reuse.
export function SegmentedBar({
  segments,
  formatValue,
}: {
  segments: Segment[];
  formatValue: (v: number) => string;
}) {
  const positive = segments.filter((s) => s.value > 0);
  const total = positive.reduce((sum, s) => sum + s.value, 0);
  return (
    <div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-panel-alt border border-border">
        {total > 0 &&
          positive.map((s) => (
            <div
              key={s.label}
              className={TONE_BAR[s.tone]}
              style={{ width: `${(s.value / total) * 100}%` }}
              title={`${s.label}: ${formatValue(s.value)}`}
            />
          ))}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1.5 text-[0.68rem]">
        {segments.map((s) => (
          <span key={s.label} className="inline-flex items-center gap-1.5">
            <span className={`w-1.5 h-1.5 rounded-full inline-block flex-shrink-0 ${TONE_BAR[s.tone]}`} />
            <span className="text-dim">{s.label}</span>
            <span className="font-semibold tabular-nums">{formatValue(s.value)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
