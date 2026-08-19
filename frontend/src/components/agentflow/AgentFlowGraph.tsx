import { useEffect, useRef } from "react";
import { ReactFlow, Background, BackgroundVariant, type Node, type Edge, type ReactFlowInstance } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { NODE_TYPES } from "./nodes";

/* Shared, deliberately non-interactive React Flow mount. QAMC's primary
 * Decision Room is a fixed, "deliberately composed" surface (the same
 * principle that keeps Dockview out of the primary cockpit) — React Flow's
 * real interactivity (drag/pan/zoom/connect) is switched off here even
 * though the library is fully capable of it, so this renders as a fixed
 * diagram, not an editable canvas.
 *
 * `fitView` only frames the topology once, at mount — the same class of
 * bug Stage 6g already found and fixed for the price chart's `autoSize`.
 * On iPad, DecisionRoomPanel mounts inside the Candidates/Chart/Decision
 * Room tab strip's `hidden` pane (App.tsx's `mobilePane` state defaults to
 * "watchlist"), so React Flow's one-time `fitView` measures a 0x0
 * container and collapses the graph into a corner; switching to the
 * Decision Room tab later never re-triggers it. Fixed the same way the
 * chart was: an explicit `ResizeObserver` on the wrapping element calls
 * `fitView()` again on every real size change, including the 0->nonzero
 * transition a hidden->visible tab switch produces. */
export function AgentFlowGraph({ nodes, edges, height = 260 }: { nodes: Node[]; edges: Edge[]; height?: number }) {
  const instanceRef = useRef<ReactFlowInstance | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);

  function handleInit(instance: ReactFlowInstance) {
    instanceRef.current = instance;
    if (!containerRef.current || resizeObserverRef.current) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect || rect.width <= 0 || rect.height <= 0) return;
      instanceRef.current?.fitView({ padding: 0.2 });
    });
    observer.observe(containerRef.current);
    resizeObserverRef.current = observer;
  }

  // The observer is created lazily inside `onInit` (React Flow's own init
  // callback, not a React lifecycle hook), so nothing else disconnects it —
  // same disconnect-on-unmount contract PriceChartPanel/EChart already use
  // for their own ResizeObservers.
  useEffect(() => {
    return () => {
      resizeObserverRef.current?.disconnect();
      resizeObserverRef.current = null;
    };
  }, []);

  return (
    <div ref={containerRef} style={{ height }} className="rounded-lg border border-border bg-panel-inset overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        onInit={handleInit}
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
