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
import { AgentPromptViewer } from "./AgentPromptViewer";

const agentColumn = createColumnHelper<AgentLogItem>();

function AgentLogsTable({
  detail, selectedId, onSelect,
}: {
  detail: RunDetailResponse;
  selectedId: number | null;
  onSelect: (id: number | null) => void;
}) {
  const columns = useMemo(() => [
    agentColumn.accessor("agent_name", { header: "Agent" }),
    agentColumn.accessor((item) => `${item.actual_provider || "—"} / ${item.model || "—"}`, {
      id: "route", header: "Provider / model",
      cell: (info) => <span className={info.row.original.requested_provider && info.row.original.actual_provider && info.row.original.requested_provider !== info.row.original.actual_provider ? "font-bold text-warn" : ""}>{info.getValue()}</span>,
    }),
    agentColumn.accessor("status", { header: "Status", cell: (info) => <Pill text={info.getValue() || "unknown"} /> }),
    agentColumn.accessor("cost_usd", { header: "Cost", cell: (info) => fmtMoney(info.getValue()) }),
    agentColumn.accessor("latency_s", { header: "Latency", cell: (info) => info.getValue() === null ? "—" : `${fmtNum(info.getValue())}s` }),
    // The prompt has been persisted and served all along; nothing offered a
    // way in. This column is that way in.
    agentColumn.display({
      id: "prompt",
      header: "Read",
      cell: (info) => {
        const item = info.row.original;
        const chars = (item.input_message ?? "").length;
        if (!chars && !(item.full_response ?? "").length) return <span className="text-dim">—</span>;
        const open = selectedId === item.id;
        return (
          <button
            type="button"
            className="underline hover:text-accent"
            aria-expanded={open}
            onClick={() => onSelect(open ? null : item.id)}
          >
            {open ? "hide" : "view"}
          </button>
        );
      },
    }),
  ] as LegacyColumnDef<AgentLogItem, unknown>[], [selectedId, onSelect]);
  if (!detail.agent_logs.length) return <StateMessage text="No agent calls logged for this run." />;
  const selected = detail.agent_logs.find((item) => item.id === selectedId) ?? null;
  return (
    <div>
      <DataTable data={detail.agent_logs} columns={columns} getRowId={(item) => String(item.id)} initialSorting={[{ id: "agent_name", desc: false }]} />
      {selected && (
        <div className="mt-3 border-t border-border pt-3">
          <div className="mb-2 text-[0.75rem] uppercase tracking-wide text-dim">
            What {selected.agent_name} read
          </div>
          <AgentPromptViewer log={selected} />
        </div>
      )}
    </div>
  );
}

export function RunDetailModal({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [funnel, setFunnel] = useState<RunFunnelResponse | null>(null);
  const [detail, setDetail] = useState<RunDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openLogId, setOpenLogId] = useState<number | null>(null);
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
            {[<AgentLogsTable key="table" detail={detail} selectedId={openLogId} onSelect={setOpenLogId} />]}
          </EvidenceSection>
          <EvidenceSection title="Persisted lifecycle events">
            {[<LifecycleTimeline key="lifecycle" events={funnel.pipeline_events ?? []} />]}
          </EvidenceSection>
        </div>
      )}
    </Modal>
  );
}
