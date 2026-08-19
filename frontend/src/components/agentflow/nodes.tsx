import { Handle, Position } from "@xyflow/react";
import { Pill } from "../ui/Pill";

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
const TONE_BG: Record<NodeTone, string> = {
  pos: "bg-pos/10",
  warn: "bg-warn/10",
  neg: "bg-neg/10",
  dim: "bg-panel-alt",
  agent: "bg-agent/10",
};
const TONE_TEXT: Record<NodeTone, string> = {
  pos: "text-pos",
  warn: "text-warn",
  neg: "text-neg",
  dim: "text-dim",
  agent: "text-agent",
};

const CONVICTION_PCT: Record<string, number> = { high: 92, medium: 58, low: 28 };
function confidenceTone(pct: number | undefined): NodeTone {
  if (pct === undefined) return "dim";
  if (pct >= 70) return "pos";
  if (pct >= 40) return "warn";
  return "neg";
}

export interface SpecialistNodeData extends Record<string, unknown> {
  role: string;
  subtitle?: string;
  direction: "bullish" | "bearish" | "neutral";
  conviction: string | null;
  reasoning: string;
  alignment?: { label: string; tone: "pos" | "warn" | "neg" } | null;
  onClick?: () => void;
}

export function SpecialistNode({ data }: { data: SpecialistNodeData }) {
  const pct = data.conviction ? CONVICTION_PCT[data.conviction] : undefined;
  const tone = confidenceTone(pct);
  const dirColor = data.direction === "bullish" ? "text-pos" : data.direction === "bearish" ? "text-neg" : "text-dim";
  return (
    <div
      role={data.onClick ? "button" : undefined}
      onClick={data.onClick}
      className={`w-[192px] rounded-lg border-l-4 border ${TONE_BORDER[tone]} border-l-agent bg-panel px-2.5 py-2 shadow-sm ${
        data.onClick ? "cursor-pointer hover:border-agent/70" : ""
      }`}
    >
      <Handle type="target" position={Position.Left} className={INVISIBLE_HANDLE} />
      <Handle type="source" position={Position.Right} className={INVISIBLE_HANDLE} />
      <div className="flex items-center justify-between gap-1.5">
        <span className="font-bold text-[0.74rem] leading-tight truncate">{data.role}</span>
        <span className={`text-[0.85rem] font-bold ${dirColor}`}>
          {data.direction === "bullish" ? "▲" : data.direction === "bearish" ? "▼" : "•"}
        </span>
      </div>
      {data.subtitle && <div className="text-[0.62rem] text-dim truncate mt-0.5">{data.subtitle}</div>}
      {pct !== undefined && (
        <div className="mt-1.5">
          <div className="flex items-center justify-between text-[0.58rem] text-dim mb-0.5">
            <span>Confidence</span>
            <span className={`font-bold ${TONE_TEXT[tone]}`}>{data.conviction?.toUpperCase()}</span>
          </div>
          <div className="h-1 w-full rounded-full bg-panel-inset overflow-hidden">
            <div className={`h-full rounded-full ${tone === "pos" ? "bg-pos" : tone === "warn" ? "bg-warn" : "bg-neg"}`} style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}
      <p className="text-[0.66rem] text-dim leading-snug mt-1.5 line-clamp-2">{data.reasoning}</p>
      {data.alignment && (
        <div className={`text-[0.6rem] font-semibold mt-1 ${TONE_TEXT[data.alignment.tone]}`}>{data.alignment.label}</div>
      )}
    </div>
  );
}

export interface StageNodeData extends Record<string, unknown> {
  label: string;
  tone: NodeTone;
  statusText: string;
  caption?: string;
}

export function StageNode({ data }: { data: StageNodeData }) {
  return (
    <div className={`w-[168px] rounded-lg border ${TONE_BORDER[data.tone]} ${TONE_BG[data.tone]} px-2.5 py-2`}>
      <Handle type="target" position={Position.Left} className={INVISIBLE_HANDLE} />
      <Handle type="source" position={Position.Right} className={INVISIBLE_HANDLE} />
      <div className="font-bold text-[0.78rem] leading-tight">{data.label}</div>
      <div className={`text-[0.62rem] font-semibold uppercase tracking-wide mt-0.5 ${TONE_TEXT[data.tone]}`}>{data.statusText}</div>
      {data.caption && <div className="text-[0.66rem] text-dim mt-1 leading-snug line-clamp-2">{data.caption}</div>}
    </div>
  );
}

export interface GateNodeData extends Record<string, unknown> {
  tone: NodeTone;
  statusText: string;
  caption?: string;
}

// The one node in the graph that is not a rounded rectangle — a hexagonal
// "hard interlock" outline (clip-path), thick double-toned border, and a
// faint diagonal hazard-stripe fill. Deliberately reads as a physically
// different kind of object, the same way a real circuit-breaker/interlock
// is drawn differently from an ordinary relay in a schematic: this is the
// one non-negotiable, non-LLM authority in the chain.
export function GateNode({ data }: { data: GateNodeData }) {
  const stripe =
    data.tone === "neg"
      ? "rgb(var(--c-red) / 0.14)"
      : data.tone === "pos"
      ? "rgb(var(--c-green) / 0.14)"
      : "rgb(var(--c-amber) / 0.14)";
  return (
    <div
      className={`w-[176px] py-3 px-3.5 border-[3px] ${TONE_BORDER[data.tone]}`}
      style={{
        clipPath: "polygon(12% 0%, 88% 0%, 100% 50%, 88% 100%, 12% 100%, 0% 50%)",
        background: `repeating-linear-gradient(135deg, ${stripe}, ${stripe} 6px, transparent 6px, transparent 12px), rgb(var(--c-surface))`,
      }}
    >
      <Handle type="target" position={Position.Left} className={INVISIBLE_HANDLE} style={{ left: "10%" }} />
      <Handle type="source" position={Position.Right} className={INVISIBLE_HANDLE} style={{ right: "10%" }} />
      <div className="text-center">
        <div className="text-[0.58rem] uppercase tracking-wider text-dim font-bold">Deterministic gate</div>
        <div className="text-[0.6rem] uppercase tracking-wide text-faint font-semibold">Final authority</div>
        <div className={`text-[0.68rem] font-extrabold uppercase tracking-wide mt-1 ${TONE_TEXT[data.tone]}`}>{data.statusText}</div>
      </div>
    </div>
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
