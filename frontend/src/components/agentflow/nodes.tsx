import { Badge, Card, type Color } from "@tremor/react";
import { Handle, Position } from "@xyflow/react";
import { Pill } from "../ui/Pill";
import { LevelBar } from "../ui/Meter";

/* Custom React Flow node types for the QAMC agent-topology graph — the
 * Decision Room's replacement for "five stacked rectangles." Two node
 * families:
 *
 * - `SpecialistNode`: one real node per specialist that produced evidence
 *   (Tech/Earnings/News), used only in the per-candidate graph where real
 *   fan-in exists. Threshold-colored (not just direction-colored)
 *   confidence — the sh1ftmaker/helm AIDecisionFeed visual grammar this
 *   redesign is explicitly modeled on.
 * - `StageNode`: the generic PM / AI Risk / Execution box, used at both
 *   the per-candidate and run-level aggregate zoom levels.
 * - `GateNode`: the Deterministic Gate — a categorically different SHAPE
 *   (a hexagonal "hard interlock" outline, not just a thicker border),
 *   because it is categorically different: the one non-LLM, non-negotiable
 *   authority in the chain.
 *
 * Every `Handle` is present (React Flow edges need a connection point) but
 * visually invisible and non-interactive — these graphs render fixed
 * topology, never an editable one.
 */

const INVISIBLE_HANDLE = "opacity-0 pointer-events-none w-1 h-1";

export type NodeTone = "pos" | "warn" | "neg" | "dim" | "agent";

const TONE_BORDER: Record<NodeTone, string> = {
  pos: "border-pos/60",
  warn: "border-warn/60",
  neg: "border-neg/60",
  dim: "border-border-strong",
  agent: "border-agent/60",
};
const TONE_COLOR: Record<NodeTone, Color> = {
  pos: "emerald",
  warn: "amber",
  neg: "rose",
  dim: "slate",
  agent: "violet",
};
const CONVICTION_TONE: Record<string, NodeTone> = { high: "pos", medium: "warn", low: "neg" };
function confidenceTone(conviction: string | null): NodeTone {
  return conviction ? CONVICTION_TONE[conviction] ?? "dim" : "dim";
}

export interface SpecialistNodeData extends Record<string, unknown> {
  role: string;
  subtitle?: string;
  direction: "bullish" | "bearish" | "neutral";
  conviction: string | null;
  reasoning: string;
  alignment?: { label: string; tone: "pos" | "warn" | "neg" } | null;
  onClick?: () => void;
  /** True for buildGraph.ts's "vertical" stacked top-to-bottom rail
   * layout (currently unused — see that file's comment). Edge handles
   * must face the direction nodes actually stack in — a hardcoded
   * Left/Right pair on vertically-stacked nodes forces React Flow's bezier
   * edges to swoop out sideways and back in to reach a target directly
   * below, reading as crossed/broken connectors instead of a clean
   * top-to-bottom chain. */
  vertical?: boolean;
}

export function SpecialistNode({ data }: { data: SpecialistNodeData }) {
  const tone = confidenceTone(data.conviction);
  const dirColor = data.direction === "bullish" ? "text-pos" : data.direction === "bearish" ? "text-neg" : "text-dim";
  const targetPos = data.vertical ? Position.Top : Position.Left;
  const sourcePos = data.vertical ? Position.Bottom : Position.Right;
  return (
    <Card
      role={data.onClick ? "button" : undefined}
      onClick={data.onClick}
      decoration="left"
      decorationColor="violet"
      className={`w-[260px] !bg-panel !p-3 !ring-border ${data.onClick ? "cursor-pointer hover:!ring-violet-500/70" : ""}`}
    >
      <Handle type="target" position={targetPos} className={INVISIBLE_HANDLE} />
      <Handle type="source" position={sourcePos} className={INVISIBLE_HANDLE} />
      <div className="flex items-center justify-between gap-1.5">
        <span className="font-bold text-[0.8125rem] leading-tight truncate">{data.role}</span>
        <Badge color={data.direction === "bullish" ? "emerald" : data.direction === "bearish" ? "rose" : "slate"} size="xs">
          <span className={dirColor}>{data.direction === "bullish" ? "▲" : data.direction === "bearish" ? "▼" : "•"} {data.direction}</span>
        </Badge>
      </div>
      {data.subtitle && <div className="text-[0.8125rem] text-dim truncate mt-0.5">{data.subtitle}</div>}
      {data.conviction && (
        <div className="mt-1.5">
          <div className="flex items-center justify-between text-meta mb-0.5">
            <span>Confidence</span>
            <Badge color={TONE_COLOR[tone]} size="xs">{data.conviction}</Badge>
          </div>
          <LevelBar level={data.conviction} tone={tone === "pos" ? "pos" : tone === "warn" ? "warn" : tone === "neg" ? "neg" : "dim"} />
        </div>
      )}
      <p className="text-[0.8125rem] text-dim leading-snug mt-1.5 line-clamp-2">{data.reasoning}</p>
      {data.alignment && (
        <Badge color={TONE_COLOR[data.alignment.tone]} size="xs" className="mt-2">{data.alignment.label}</Badge>
      )}
    </Card>
  );
}

export interface StageNodeData extends Record<string, unknown> {
  label: string;
  tone: NodeTone;
  statusText: string;
  caption?: string;
  /** See SpecialistNodeData.vertical. */
  vertical?: boolean;
}

export function StageNode({ data }: { data: StageNodeData }) {
  const targetPos = data.vertical ? Position.Top : Position.Left;
  const sourcePos = data.vertical ? Position.Bottom : Position.Right;
  return (
    <Card
      decoration="left"
      decorationColor={TONE_COLOR[data.tone]}
      className="w-[240px] !bg-panel !p-3 !ring-border"
    >
      <Handle type="target" position={targetPos} className={INVISIBLE_HANDLE} />
      <Handle type="source" position={sourcePos} className={INVISIBLE_HANDLE} />
      <div className="font-bold text-[0.875rem] leading-tight">{data.label}</div>
      <Badge color={TONE_COLOR[data.tone]} size="xs" className="mt-1">{data.statusText}</Badge>
      {data.caption && <div className="text-[0.75rem] text-dim mt-1 leading-snug line-clamp-2">{data.caption}</div>}
    </Card>
  );
}

export interface GateNodeData extends Record<string, unknown> {
  tone: NodeTone;
  statusText: string;
  caption?: string;
  /** See SpecialistNodeData.vertical. */
  vertical?: boolean;
}

// Cockpit trader rework, item 5: this used to be a hand-drawn hexagonal
// "hard interlock" outline (CSS clip-path) with a diagonal hazard-stripe
// fill — flagged by the owner as unprofessional, bespoke ASCII-art-style
// graphics, against a standing rule of using only the project's existing,
// standardized component library. Rebuilt as the same Tremor `Card` every
// other node in this graph uses (StageNode above), still visually
// distinguished from an ordinary stage — a stronger border weight and an
// explicit "FINAL AUTHORITY" badge — but as a standard card, not a custom
// shape.
export function GateNode({ data }: { data: GateNodeData }) {
  const targetPos = data.vertical ? Position.Top : Position.Left;
  const sourcePos = data.vertical ? Position.Bottom : Position.Right;
  return (
    <Card
      decoration="left"
      decorationColor={TONE_COLOR[data.tone]}
      className={`w-[248px] !bg-panel !p-3 !ring-2 !ring-border-strong`}
    >
      <Handle type="target" position={targetPos} className={INVISIBLE_HANDLE} />
      <Handle type="source" position={sourcePos} className={INVISIBLE_HANDLE} />
      <div className="flex items-center justify-between gap-1.5">
        <span className="font-bold text-[0.875rem] leading-tight">Deterministic gate</span>
        <Badge color="slate" size="xs">Final authority</Badge>
      </div>
      <Badge color={TONE_COLOR[data.tone]} size="xs" className="mt-1.5">{data.statusText}</Badge>
      {data.caption && <div className="text-[0.75rem] text-dim mt-1 leading-snug line-clamp-2">{data.caption}</div>}
    </Card>
  );
}

export const NODE_TYPES = {
  specialist: SpecialistNode,
  stage: StageNode,
  gate: GateNode,
};

// Re-exported so callers can render the same aligned-with-consensus /
// diverges-from-majority pill next to a specialist card outside the graph
// too (e.g. in the drill-down's non-graph evidence list) without importing
// from a component that's about to be retired.
export { Pill };
