import { useMemo } from "react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { RunSummary } from "../api/client";
import { fmtMoney, fmtNum, fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { DataTable } from "./ui/DataTable";
import { useModalActions } from "../context/ModalContext";

const columnHelper = createColumnHelper<RunSummary>();

export function RunsPanel({ runs, error, loading }: { runs: RunSummary[]; error: string | null; loading: boolean }) {
  const { openRunDetail } = useModalActions();
  const columns = useMemo(() => [
    columnHelper.accessor("run_id", { header: "Run ID", cell: (info) => <span className="font-bold text-accent">{info.getValue()}</span> }),
    columnHelper.accessor("session_prefix", { header: "Session", cell: (info) => info.getValue() || "—" }),
    columnHelper.accessor("first_timestamp", { header: "First call", cell: (info) => fmtTime(info.getValue()) }),
    columnHelper.accessor("agent_count", { header: "Agents", cell: (info) => fmtNum(info.getValue(), 0) }),
    columnHelper.accessor("total_cost_usd", { header: "Cost", cell: (info) => fmtMoney(info.getValue()) }),
  ] as LegacyColumnDef<RunSummary, unknown>[], []);
  const status = error ? "error" : loading ? "loading" : "ok";
  return (
    <Panel title="Runs" subtitle="Select a run to inspect its canonical funnel, lifecycle and candidate evidence." status={status} full>
      {error && <StateMessage text={`Could not load runs: ${error}`} error />}
      {!error && runs.length === 0 && <StateMessage text="No runs recorded yet." />}
      {!error && runs.length > 0 && <DataTable data={runs} columns={columns} getRowId={(run) => run.run_id} initialSorting={[{ id: "first_timestamp", desc: true }]} onRowClick={(run) => openRunDetail(run.run_id)} />}
    </Panel>
  );
}
