import { useEffect, useState } from "react";
import { api, RunDetailResponse, RunFunnelResponse } from "../api/client";
import { fmtMoney, fmtNum } from "../lib/format";
import { Modal } from "./ui/Modal";
import { Pill } from "./ui/Pill";
import { EvidenceSection } from "./ui/Evidence";
import { StateMessage } from "./ui/Panel";
import { FunnelSteps } from "./funnelShared";
import { useModalActions } from "../context/ModalContext";

const STATE_LABELS: Record<string, string> = {
  executed: "EXECUTED",
  proposed_not_executed: "PROPOSED — NOT EXECUTED",
  hard_risk_block: "DETERMINISTIC GATE BLOCKED",
  no_proposal: "NO TRADE — PM STAYED NEUTRAL",
  no_candidates: "NO CANDIDATES CONSIDERED",
};

function AgentLogsTable({ detail }: { detail: RunDetailResponse }) {
  if (!detail.agent_logs.length) return <StateMessage text="No agent calls logged for this run." />;
  return (
    <table>
      <thead>
        <tr>
          <th>Agent</th>
          <th>Provider / model</th>
          <th>Status</th>
          <th>Cost</th>
          <th>Latency</th>
        </tr>
      </thead>
      <tbody>
        {detail.agent_logs.map((a) => {
          const changed = a.requested_provider && a.actual_provider && a.requested_provider !== a.actual_provider;
          return (
            <tr key={a.id}>
              <td>{a.agent_name}</td>
              <td className={changed ? "text-warn font-bold" : ""}>
                {a.actual_provider || "—"} / {a.model || "—"}
              </td>
              <td>
                <Pill text={a.status || "unknown"} />
              </td>
              <td>{fmtMoney(a.cost_usd)}</td>
              <td>{a.latency_s !== null && a.latency_s !== undefined ? `${fmtNum(a.latency_s)}s` : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function RunDetailModal({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [funnel, setFunnel] = useState<RunFunnelResponse | null>(null);
  const [detail, setDetail] = useState<RunDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { openCandidateDetail } = useModalActions();

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.runFunnel(runId), api.runDetail(runId)])
      .then(([f, d]) => {
        if (cancelled) return;
        setFunnel(f);
        setDetail(d);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  return (
    <Modal breadcrumb={<span className="font-bold">Run {runId}</span>} onClose={onClose}>
      {error && <StateMessage text={`Could not load run ${runId}: ${error}`} error />}
      {!error && !funnel && <StateMessage text="Loading run…" />}
      {funnel && detail && (
        <div>
          <div className="flex items-center gap-3 flex-wrap mb-3">
            <span className="pill border px-3 py-1 bg-panel-alt">{STATE_LABELS[funnel.decision_state]}</span>
          </div>
          <FunnelSteps funnel={funnel} />
          <EvidenceSection title="Candidates">
            {[
              funnel.candidates.length ? (
                <div key="chips">
                  {funnel.candidates.map((c) => (
                    <button
                      key={c.symbol}
                      type="button"
                      className="candidate-chip"
                      onClick={() => openCandidateDetail(runId, c.symbol)}
                    >
                      <span>{c.symbol}</span>
                      <Pill text={c.direction} />
                      {c.is_bearish_hedge && <Pill text="bearish_hedge" />}
                    </button>
                  ))}
                </div>
              ) : null,
            ]}
          </EvidenceSection>
          <EvidenceSection title="Agent calls this run">
            {[<AgentLogsTable key="table" detail={detail} />]}
          </EvidenceSection>
        </div>
      )}
    </Modal>
  );
}
