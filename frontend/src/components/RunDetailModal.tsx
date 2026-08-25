import { useEffect, useMemo, useState } from "react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { api, AgentLogItem, RunDetailResponse, RunFunnelResponse } from "../api/client";
import { fmtMoney, fmtNum } from "../lib/format";
import { Modal } from "./ui/Modal";
import { Pill } from "./ui/Pill";
import { EvidenceSection } from "./ui/Evidence";
import { StateMessage } from "./ui/Panel";
import { STATE_LABELS } from "./funnelShared";
import { AgentFlowGraph } from "./agentflow/AgentFlowGraph";
import { buildRunGraph } from "./agentflow/buildGraph";
import { useModalActions } from "../context/ModalContext";
import { LifecycleTimeline } from "./LifecycleTimeline";
import { DataTable } from "./ui/DataTable";

const agentColumn = createColumnHelper<AgentLogItem>();

function AgentLogsTable({ detail }: { detail: RunDetailResponse }) {
  const columns = useMemo(() => [
    agentColumn.accessor("agent_name", { header: "Agent" }),
    agentColumn.accessor((item) => `${item.actual_provider || "—"} / ${item.model || "—"}`, {
      id: "route", header: "Provider / model",
      cell: (info) => <span className={info.row.original.requested_provider && info.row.original.actual_provider && info.row.original.requested_provider !== info.row.original.actual_provider ? "font-bold text-warn" : ""}>{info.getValue()}</span>,
    }),
    agentColumn.accessor("status", { header: "Status", cell: (info) => <Pill text={info.getValue() || "unknown"} /> }),
    agentColumn.accessor("cost_usd", { header: "Cost", cell: (info) => fmtMoney(info.getValue()) }),
    agentColumn.accessor("latency_s", { header: "Latency", cell: (info) => info.getValue() === null ? "—" : `${fmtNum(info.getValue())}s` }),
  ] as LegacyColumnDef<AgentLogItem, unknown>[], []);
  if (!detail.agent_logs.length) return <StateMessage text="No agent calls logged for this run." />;
  return <DataTable data={detail.agent_logs} columns={columns} getRowId={(item) => String(item.id)} initialSorting={[{ id: "agent_name", desc: false }]} />;
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
          <AgentFlowGraph {...buildRunGraph(funnel, "horizontal")} height={180} />
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
          <EvidenceSection title="Persisted lifecycle events">
            {[<LifecycleTimeline key="lifecycle" events={funnel.pipeline_events ?? []} />]}
          </EvidenceSection>
        </div>
      )}
    </Modal>
  );
}
