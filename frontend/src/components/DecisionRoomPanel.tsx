import { RunFunnelResponse } from "../api/client";
import { fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { Card, KV } from "./ui/Evidence";
import { DecisionFlowDiagram } from "./DecisionFlowDiagram";
import { STATE_LABELS, STATE_COLORS, buildFunnelStages } from "./funnelShared";
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
}: {
  funnel: RunFunnelResponse | null;
  loading: boolean;
  error: string | null;
}) {
  const { openRunDetail } = useModalActions();
  const status = error ? "error" : loading ? "loading" : "ok";

  return (
    <Panel title="Decision Room — latest run" status={status} accent>
      {error && <StateMessage text={`Could not load latest decision: ${error}`} error />}
      {!error && !funnel && <StateMessage text="Loading…" />}
      {funnel && (
        <div className="flex flex-col gap-3">
          <div>
            <span className={`pill border text-[0.76rem] px-2.5 py-1 ${STATE_COLORS[funnel.decision_state]}`}>
              {STATE_LABELS[funnel.decision_state]}
            </span>
            <div className="text-dim text-[0.72rem] mt-1.5">
              run {funnel.run_id}
              {funnel.session_prefix ? ` (${funnel.session_prefix})` : ""} &middot; {fmtTime(funnel.timestamp)}
            </div>
          </div>

          <DecisionFlowDiagram stages={buildFunnelStages(funnel)} layout="vertical" />

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
