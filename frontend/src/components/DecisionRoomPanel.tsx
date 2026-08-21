import { RunFunnelResponse } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { Card, KV } from "./ui/Evidence";
import { AgentFlowGraph } from "./agentflow/AgentFlowGraph";
import { buildRunGraph } from "./agentflow/buildGraph";
import { useModalActions } from "../context/ModalContext";

/* The cockpit's "Decision Room" — a narrow-column condensation of what
 * used to be the full-width DecisionFunnelPanel. Same underlying data
 * (RunFunnelResponse, same buildFunnelStages derivation), just laid out
 * for a ~340px rail: single-column cards, clamped excerpt text, and a
 * link into RunDetailModal for the uncondensed version rather than
 * inlining everything here. */

// Local, not shared ui/Evidence.tsx's CardText — that component has no
// line-clamp option, and adding one there would affect every other panel
// using it. Long PM/Risk narrative text is exactly what would otherwise
// make this rail as tall as the old page-dump it replaces.
function ClampText({ text }: { text: string }) {
  return <p className="text-[0.8rem] mt-1.5 leading-snug line-clamp-5">{text}</p>;
}

export function DecisionRoomPanel({
  funnel,
  loading,
  error,
  updatedAt,
  isLive = true,
}: {
  funnel: RunFunnelResponse | null;
  loading: boolean;
  error: string | null;
  updatedAt?: Date | null;
  /** false when the Cockpit is pinned to an explicit historical run
   * instead of following the latest one — see App.tsx / RunTimeline. */
  isLive?: boolean;
}) {
  const { openRunDetail } = useModalActions();
  // A failed poll never blanks previously-loaded funnel data — it renders
  // as "stale" instead, with a timestamp, so the reader is never shown an
  // old EXECUTED/REJECTED verdict looking exactly like a current one. Only
  // a fetch that has NEVER succeeded (no funnel data at all) renders as a
  // bare error.
  const status = error ? (funnel ? "stale" : "error") : loading ? "loading" : "ok";

  return (
    <Panel title={isLive ? "Decision Room — latest run" : "Decision Room — pinned run"} status={status} staleSince={updatedAt} accent>
      {error && !funnel && <StateMessage text={`Could not load latest decision: ${error}`} error />}
      {!error && !funnel && <StateMessage text="Loading…" />}
      {funnel && (
        <div className="flex flex-col gap-3">
          {error && (
            <div className="text-warn text-[0.78rem] bg-warn/10 border border-warn/30 rounded-md px-2.5 py-1.5">
              Showing last known decision as of {updatedAt ? updatedAt.toLocaleTimeString() : "an earlier fetch"} — a fresh
              fetch failed: {error}
            </div>
          )}
          {/* The terminal state pill itself now lives in the full-width
              DecisionStateBanner above the cockpit body — this graph is the
              "how it got there," not a second place to restate "what." */}
          <AgentFlowGraph {...buildRunGraph(funnel)} height={420} />

          {funnel.bearish_hedge_considered && (
            <div className="state-message">A bearish inverse-ETF candidate was considered this run.</div>
          )}

          <Card title="Market regime" broader>
            {funnel.macro_context ? (
              <>
                <div className="kv-row">
                  <span className="text-dim">Regime</span>
                  <Pill text={funnel.macro_context.regime} />
                </div>
                <KV label="Equity outlook" value={funnel.macro_context.equity_outlook} />
                <KV label="Confidence" value={funnel.macro_context.confidence} />
                {funnel.macro_context.summary && <ClampText text={funnel.macro_context.summary} />}
              </>
            ) : (
              <StateMessage text="No macro regime evidence recorded for this run." />
            )}
          </Card>

          {funnel.pm_reasoning?.portfolio_view && (
            <Card title="Portfolio Manager">
              <ClampText text={funnel.pm_reasoning.portfolio_view} />
            </Card>
          )}

          {funnel.risk_verdict?.verdict && (
            <Card title="AI Risk Manager">
              <div className="kv-row">
                <span className="text-dim">Verdict</span>
                <div className="flex gap-1.5 flex-wrap justify-end">
                  <Pill text={funnel.risk_verdict.verdict.approved ? "approved" : "rejected"} />
                  <Pill text={funnel.risk_verdict.verdict.reason_category} />
                </div>
              </div>
              <ClampText text={funnel.risk_verdict.verdict.reasoning} />
            </Card>
          )}

          {funnel.decision_state === "hard_risk_block" && (
            <Card title="Deterministic gate">
              <ClampText text="The deterministic hard-risk gate blocked every candidate this run before the AI Risk Manager was ever called." />
            </Card>
          )}

          <button
            type="button"
            onClick={() => openRunDetail(funnel.run_id)}
            className="text-accent underline text-[0.78rem] text-left"
          >
            Open full run detail &rarr;
          </button>
        </div>
      )}
    </Panel>
  );
}
