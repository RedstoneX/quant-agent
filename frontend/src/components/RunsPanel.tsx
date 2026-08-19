import { RunSummary } from "../api/client";
import { fmtMoney, fmtNum, fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { useModalActions } from "../context/ModalContext";

export function RunsPanel({ runs, error, loading }: { runs: RunSummary[]; error: string | null; loading: boolean }) {
  const { openRunDetail } = useModalActions();
  const status = error ? "error" : loading ? "loading" : "ok";
  return (
    <Panel
      title="Runs"
      subtitle="Every pipeline run. Click one to see its full decision funnel and candidate evidence."
      status={status}
      full
    >
      {error && <StateMessage text={`Could not load runs: ${error}`} error />}
      {!error && runs.length === 0 && <StateMessage text="No runs recorded yet." />}
      {!error && runs.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Run ID</th>
              <th>Session</th>
              <th>First Call</th>
              <th>Agents</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr
                key={r.run_id}
                className="cursor-pointer hover:bg-panel-alt"
                tabIndex={0}
                onClick={() => openRunDetail(r.run_id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    openRunDetail(r.run_id);
                  }
                }}
              >
                <td>{r.run_id}</td>
                <td>{r.session_prefix || "—"}</td>
                <td>{fmtTime(r.first_timestamp)}</td>
                <td>{fmtNum(r.agent_count, 0)}</td>
                <td>{fmtMoney(r.total_cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
