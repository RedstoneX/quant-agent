import { ReactFlow, Background, BackgroundVariant, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { NODE_TYPES } from "./nodes";

/* Shared, deliberately non-interactive React Flow mount. QAMC's primary
 * Decision Room is a fixed, "deliberately composed" surface (the same
 * principle that keeps Dockview out of the primary cockpit) — React Flow's
 * real interactivity (drag/pan/zoom/connect) is switched off here even
 * though the library is fully capable of it, so this renders as a fixed
 * diagram, not an editable canvas. `fitView` keeps the topology framed to
 * whatever nodes/edges the caller built without hand-tuned pan/zoom state. */
export function AgentFlowGraph({ nodes, edges, height = 260 }: { nodes: Node[]; edges: Edge[]; height?: number }) {
  return (
    <div style={{ height }} className="rounded-lg border border-border bg-panel-inset overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag={false}
        panOnScroll={false}
        zoomOnScroll={false}
        zoomOnPinch={false}
        zoomOnDoubleClick={false}
        preventScrolling={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="rgb(var(--c-border))" />
      </ReactFlow>
    </div>
  );
}
