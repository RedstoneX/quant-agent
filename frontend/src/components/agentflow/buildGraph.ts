import type { Node, Edge } from "@xyflow/react";
import { CandidateDetailResponse, ConsensusSummary, RunFunnelResponse } from "../../api/client";
import { FlowStage, FlowStatus } from "./types";
import { buildFunnelStages } from "../funnelShared";
import type { GateNodeData, NodeTone, SpecialistNodeData, StageNodeData } from "./nodes";

/* Pure graph-shape builders — no rendering here, just {nodes, edges} for
 * AgentFlowGraph. Two entry points sharing the same visual vocabulary at
 * two zoom levels: `buildRunGraph` (aggregate counts, always exactly 5
 * nodes) and `buildCandidateGraph` (real per-specialist fan-in — up to 3
 * Tech/Earnings/News nodes converging on one PM node, which is the
 * information a flattened "Specialists" box previously discarded). */

const STATUS_TONE: Record<FlowStatus, NodeTone> = {
  reached: "pos",
  executed: "pos",
  approved: "pos",
  not_reached: "dim",
  modified: "warn",
  pending: "warn",
  rejected: "neg",
  blocked: "neg",
};

const STATUS_TEXT: Record<FlowStatus, string> = {
  reached: "Reached",
  executed: "Executed",
  approved: "Approved",
  not_reached: "Not reached",
  modified: "Modified",
  pending: "Pending",
  rejected: "Rejected",
  blocked: "Blocked",
};

function edgeTone(status: FlowStatus): string {
  if (status === "not_reached") return "rgb(var(--c-border-strong))";
  if (status === "rejected" || status === "blocked") return "rgb(var(--c-red))";
  if (status === "modified" || status === "pending") return "rgb(var(--c-amber))";
  return "rgb(var(--c-green))";
}

function edgeFor(id: string, source: string, target: string, status: FlowStatus, animated = false): Edge {
  return {
    id,
    source,
    target,
    animated: animated && status !== "not_reached",
    style: { stroke: edgeTone(status), strokeWidth: 2 },
  };
}

/** Run-level aggregate — always the same 5 nodes (Specialists / PM / Risk /
 * Gate / Execution). `vertical` (the default, for the ~340px Decision Room
 * rail) stacks top-to-bottom; `horizontal` (for the wider RunDetailModal)
 * runs left-to-right. */
export function buildRunGraph(
  funnel: RunFunnelResponse,
  direction: "vertical" | "horizontal" = "vertical"
): { nodes: Node[]; edges: Edge[] } {
  const stages = buildFunnelStages(funnel);
  const nodes: Node[] = stages.map((s, i) => stageToNode(s, i, direction));
  const edges: Edge[] = [];
  for (let i = 1; i < stages.length; i++) {
    edges.push(edgeFor(`e${i}`, stages[i - 1].key, stages[i].key, stages[i].status, true));
  }
  return { nodes, edges };
}

function stageToNode(s: FlowStage, index: number, direction: "vertical" | "horizontal" = "vertical"): Node {
  const tone = STATUS_TONE[s.status];
  const x = direction === "vertical" ? 90 : 40 + index * 190;
  const y = direction === "vertical" ? 40 + index * 110 : 60;
  if (s.key === "gate") {
    const data: GateNodeData = { tone, statusText: STATUS_TEXT[s.status], caption: s.caption };
    return { id: s.key, type: "gate", position: { x: x - 4, y }, data: data as unknown as Record<string, unknown>, draggable: false };
  }
  const data: StageNodeData = { label: s.label, tone, statusText: STATUS_TEXT[s.status], caption: s.caption };
  return { id: s.key, type: "stage", position: { x, y }, data: data as unknown as Record<string, unknown>, draggable: false };
}

// --- Per-candidate real fan-in graph -------------------------------------

type Direction = "bullish" | "bearish" | "neutral";

const TECH_DIRECTION: Record<string, Direction> = {
  strong_buy: "bullish",
  buy: "bullish",
  neutral: "neutral",
  sell: "bearish",
  strong_sell: "bearish",
};

interface SpecialistEntry {
  key: string;
  role: string;
  subtitle?: string;
  direction: Direction;
  conviction: string | null;
  reasoning: string;
}

function buildEntries(detail: CandidateDetailResponse): SpecialistEntry[] {
  const entries: SpecialistEntry[] = [];
  if (detail.tech) {
    entries.push({
      key: "tech",
      role: "Technical Analyst",
      direction: TECH_DIRECTION[detail.tech.rating] || "neutral",
      conviction: detail.tech.conviction,
      reasoning: detail.tech.reasoning,
    });
  }
  if (detail.earnings) {
    const impl = detail.earnings.investment_implications;
    entries.push({
      key: "earnings",
      role: "Earnings Analyst",
      direction: impl.sentiment,
      conviction: impl.conviction,
      reasoning: impl.key_thesis,
    });
  }
  detail.news_symbol.forEach((n, i) => {
    entries.push({
      key: `news-${i}`,
      role: "News Analyst",
      subtitle: n.headline,
      direction: n.sentiment,
      conviction: n.conviction,
      reasoning: n.impact_summary,
    });
  });
  return entries;
}

// Mirrors CandidateDetailModal's prior SpecialistCards derivation exactly —
// never asserts "aligned"/"diverges" beyond what the backend's own
// consensus.agreement computation supports.
function computeAlignments(
  entries: SpecialistEntry[],
  consensus: ConsensusSummary
): (SpecialistNodeData["alignment"])[] {
  if (consensus.agreement !== "aligned" && consensus.agreement !== "mixed") {
    return entries.map(() => null);
  }
  const counts: Record<string, number> = {};
  for (const e of entries) if (e.direction !== "neutral") counts[e.direction] = (counts[e.direction] || 0) + 1;
  const totalDirectional = Object.values(counts).reduce((a, b) => a + b, 0);
  return entries.map((e) => {
    if (e.direction === "neutral") return null;
    if (consensus.agreement === "aligned") return { label: "Aligned with consensus", tone: "pos" };
    const same = counts[e.direction] || 0;
    if (same <= 1) return { label: "Diverges — sole view", tone: "neg" };
    if (same * 2 > totalDirectional) return { label: `Majority (${same}/${totalDirectional})`, tone: "warn" };
    return { label: `Diverges from majority (${same}/${totalDirectional})`, tone: "warn" };
  });
}

function edgeToneForDirection(dir: Direction): string {
  if (dir === "bullish") return "rgb(var(--c-green))";
  if (dir === "bearish") return "rgb(var(--c-red))";
  return "rgb(var(--c-border-strong))";
}

/** Per-candidate graph — real fan-in: one node per specialist that actually
 * produced evidence this run (never a fabricated card for one that
 * didn't), converging on PM, then the same PM -> Risk -> Gate -> Execution
 * chain the run-level graph uses. Horizontal: specialists in a left
 * column, the linear chain running left-to-right. */
export function buildCandidateGraph(
  detail: CandidateDetailResponse,
  candidateStages: FlowStage[],
  onSpecialistClick?: (key: string) => void
): { nodes: Node[]; edges: Edge[] } {
  const entries = buildEntries(detail);
  const alignments = computeAlignments(entries, detail.consensus);
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  const specialistsReached = candidateStages.find((s) => s.key === "specialists")?.status !== "not_reached";
  const specX = 20;
  const chainStartX = 240;
  const rowSpacing = 96;
  const specTop = entries.length ? 40 + ((3 - entries.length) * rowSpacing) / 2 : 40;

  entries.forEach((e, i) => {
    const data: SpecialistNodeData = {
      role: e.role,
      subtitle: e.subtitle,
      direction: e.direction,
      conviction: e.conviction,
      reasoning: e.reasoning,
      alignment: alignments[i],
      onClick: onSpecialistClick ? () => onSpecialistClick(e.key) : undefined,
    };
    nodes.push({
      id: `spec-${e.key}`,
      type: "specialist",
      position: { x: specX, y: specTop + i * rowSpacing },
      data: data as unknown as Record<string, unknown>,
      draggable: false,
    });
    edges.push(edgeFor(`e-spec-${e.key}`, `spec-${e.key}`, "pm", specialistsReached ? "reached" : "not_reached", true));
  });

  if (!entries.length) {
    // No specialist evidence recorded — still show a single dim placeholder
    // node so the chain reads "not reached," not a mysteriously missing
    // first column.
    const data: StageNodeData = { label: "Specialists", tone: "dim", statusText: "Not reached", caption: "No evidence recorded" };
    nodes.push({ id: "spec-none", type: "stage", position: { x: specX, y: 40 }, data: data as unknown as Record<string, unknown>, draggable: false });
    edges.push(edgeFor("e-spec-none", "spec-none", "pm", "not_reached"));
  }

  const chainKeys: FlowStage["key"][] = ["pm", "risk", "gate", "exec"];
  chainKeys.forEach((key, i) => {
    const s = candidateStages.find((st) => st.key === key);
    if (!s) return;
    const node = stageToNode(s, 0);
    node.position = { x: chainStartX + i * 190, y: 96 };
    nodes.push(node);
    if (i > 0) {
      const prev = chainKeys[i - 1];
      edges.push(edgeFor(`e-${prev}-${key}`, prev, key, s.status, true));
    }
  });

  return { nodes, edges };
}
