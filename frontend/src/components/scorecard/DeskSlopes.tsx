/* Section (a) — two small slope panels, side by side.
 *
 * Drawn as plain SVG with `d3-scale` for the value->pixel mapping and
 * `d3-shape`'s `line` generator for the path. No chart library: a slope graph
 * is two x positions and a straight segment, and every charting library that
 * can draw one also brings axes, legends and tooltips this does not want.
 *
 * READING WITHOUT COLOUR. Every analyst's line is:
 *   - solid + filled end dot when it rose,
 *   - dashed + hollow end dot when it fell,
 *   - prefixed with an explicit ▲ / ▼ / · glyph in its right-hand label,
 *   - labelled with its actual value at BOTH ends.
 * Colour only repeats what those four already say. Nothing here is
 * distinguishable by hue alone, and no red element is ever placed adjacent to
 * a green one as the only difference between them.
 */

import { useMemo } from "react";
import { scaleLinear } from "d3-scale";
import { line as d3Line } from "d3-shape";
import type { SlopeRow } from "./scorecardModel";
import { monthLabel, trendGlyph } from "./scorecardModel";

const WIDTH = 460;
const HEIGHT = 250;
const PAD = { top: 34, bottom: 30, left: 104, right: 116 };
const MIN_LABEL_GAP = 14;

/** Push overlapping labels apart while keeping their order — otherwise two
 * analysts on a similar value render as one unreadable smudge. */
function declutter(values: number[]): number[] {
  const order = values.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  const out = new Array<number>(values.length);
  let last = -Infinity;
  for (const entry of order) {
    const y = Math.max(entry.y, last + MIN_LABEL_GAP);
    out[entry.i] = y;
    last = y;
  }
  return out;
}

export function SlopePanel({
  title,
  question,
  rows,
  fromMonth,
  toMonth,
  format,
  testId,
}: {
  title: string;
  /** The plain-English question this panel answers, shown under the title. */
  question: string;
  rows: SlopeRow[];
  fromMonth: string;
  toMonth: string;
  format: (v: number) => string;
  testId: string;
}) {
  const geometry = useMemo(() => {
    const values = rows.flatMap((r) => (r.hasFrom ? [r.from, r.to] : [r.to]));
    const min = Math.min(0, ...values);
    const max = Math.max(0, ...values);
    // A flat desk (every value identical) would collapse the scale; pad it so
    // the lines still render on a readable band instead of one overlaid row.
    const span = max - min || 1;
    const y = scaleLinear()
      .domain([min - span * 0.12, max + span * 0.12])
      .range([HEIGHT - PAD.bottom, PAD.top]);
    const x0 = PAD.left;
    const x1 = WIDTH - PAD.right;
    const path = d3Line<[number, number]>()
      .x((d) => d[0])
      .y((d) => d[1]);
    const leftLabels = declutter(rows.map((r) => (r.hasFrom ? y(r.from) : y(r.to))));
    const rightLabels = declutter(rows.map((r) => y(r.to)));
    return { y, x0, x1, path, leftLabels, rightLabels, zero: y(0) };
  }, [rows]);

  const { y, x0, x1, path, leftLabels, rightLabels, zero } = geometry;

  return (
    <figure className="m-0 rounded-lg border border-border bg-panel-inset p-3" data-testid={testId}>
      <figcaption className="mb-2">
        <h3 className="m-0 text-[length:var(--fs-subhead)] font-semibold text-ink">{title}</h3>
        <p className="m-0 mt-1 text-[length:var(--fs-meta)] leading-snug text-dim">{question}</p>
      </figcaption>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full"
        role="img"
        aria-label={`${title}. ${rows
          .map((r) =>
            r.hasFrom
              ? `${r.analyst}: ${format(r.from)} in ${monthLabel(fromMonth)}, ${format(r.to)} in ${monthLabel(toMonth)}.`
              : `${r.analyst}: no settled calls until ${monthLabel(r.firstMonth)}, ${format(r.to)} now.`,
          )
          .join(" ")}`}
      >
        {/* The two dates, named — never left to a legend. */}
        <text x={x0} y={PAD.top - 14} textAnchor="middle" className="fill-dim" fontSize={11}>
          {monthLabel(fromMonth)}
        </text>
        <text x={x1} y={PAD.top - 14} textAnchor="middle" className="fill-dim" fontSize={11}>
          {monthLabel(toMonth)}
        </text>
        <line x1={x0} y1={PAD.top - 6} x2={x0} y2={HEIGHT - PAD.bottom} stroke="rgb(var(--c-border))" />
        <line x1={x1} y1={PAD.top - 6} x2={x1} y2={HEIGHT - PAD.bottom} stroke="rgb(var(--c-border))" />
        {/* Zero: the reference every value on this panel is read against. */}
        <line
          x1={x0 - 8}
          y1={zero}
          x2={x1 + 8}
          y2={zero}
          stroke="rgb(var(--c-ink-faint))"
          strokeDasharray="2 3"
        />
        <text x={x1 + 12} y={zero + 3} className="fill-faint" fontSize={9}>
          zero
        </text>

        {rows.map((row, index) => {
          const rose = row.change > 0;
          const fell = row.change < 0;
          const tone = rose ? "rgb(var(--c-green))" : fell ? "rgb(var(--c-red))" : "rgb(var(--c-ink-dim))";
          const yTo = y(row.to);
          const yFrom = row.hasFrom ? y(row.from) : yTo;
          return (
            <g key={row.analyst}>
              {row.hasFrom && (
                <path
                  d={path([[x0, yFrom], [x1, yTo]]) ?? undefined}
                  fill="none"
                  stroke={tone}
                  strokeWidth={1.6}
                  // Shape, not hue: a falling line is dashed everywhere on
                  // this page, a rising one solid.
                  strokeDasharray={fell ? "5 3" : undefined}
                />
              )}
              {row.hasFrom && <circle cx={x0} cy={yFrom} r={3} fill="rgb(var(--c-ink-dim))" />}
              <circle
                cx={x1}
                cy={yTo}
                r={3.6}
                // Filled for up, hollow for down — the second shape channel.
                fill={rose ? tone : "rgb(var(--c-surface-inset))"}
                stroke={tone}
                strokeWidth={1.6}
              />
              {row.hasFrom ? (
                <text
                  x={x0 - 8}
                  y={leftLabels[index] + 3}
                  textAnchor="end"
                  className="fill-dim font-mono"
                  fontSize={10.5}
                >
                  {format(row.from)}
                </text>
              ) : (
                <text
                  x={x0 - 8}
                  y={leftLabels[index] + 3}
                  textAnchor="end"
                  className="fill-faint"
                  fontSize={9.5}
                >
                  none yet
                </text>
              )}
              <text x={x1 + 8} y={rightLabels[index] + 3} className="fill-ink" fontSize={10.5}>
                <tspan className="font-mono" aria-hidden="true">
                  {trendGlyph(row.change)}
                </tspan>{" "}
                <tspan className="font-semibold">{row.analyst}</tspan>{" "}
                <tspan className="font-mono fill-dim">{format(row.to)}</tspan>
              </text>
            </g>
          );
        })}
      </svg>
      <ul className="mt-2 list-none space-y-0.5 p-0 text-[length:var(--fs-micro)] text-faint">
        {rows
          .filter((r) => !r.hasFrom)
          .map((r) => (
            <li key={r.analyst}>
              {r.analyst} has no record at the earlier date — its first settled call was in{" "}
              {monthLabel(r.firstMonth)}.
            </li>
          ))}
      </ul>
    </figure>
  );
}
