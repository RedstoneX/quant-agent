import { useState } from "react";
import { api, SearchResponse } from "../api/client";
import { fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { useModalActions } from "../context/ModalContext";

export function SearchPanel() {
  const [q, setQ] = useState("");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const { openRunDetail } = useModalActions();

  async function runSearch() {
    const term = q.trim();
    if (!term) {
      setResult(null);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const r = await api.search(term, 50);
      setResult(r);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  const status = error ? "error" : loading ? "loading" : "ok";
  const noMatches = result && result.trades.length === 0 && result.agent_logs.length === 0;

  return (
    <Panel
      title="Search"
      status={status}
      actions={
        <div className="flex gap-1.5">
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") runSearch();
            }}
            placeholder="Search trade reasoning, agent names, models, summaries…"
            className="bg-panel-alt border border-border rounded text-[0.78rem] px-2 py-1 min-w-[220px]"
          />
          <button
            type="button"
            onClick={runSearch}
            className="bg-accent text-panel rounded text-[0.78rem] font-semibold px-2.5 py-1"
          >
            Search
          </button>
        </div>
      }
    >
      {error && <StateMessage text={`Search failed: ${error}`} error />}
      {!error && !result && <StateMessage text="Type a search term above." />}
      {!error && noMatches && <StateMessage text={`No matches for "${result?.query}".`} />}
      {!error && result && !noMatches && (
        <div className="flex flex-col gap-4">
          {result.trades.length > 0 && (
            <div>
              <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-1.5">
                Trade hits ({result.trades.length})
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Symbol</th>
                    <th>Action</th>
                    <th>Reasoning</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.map((h) => (
                    <tr
                      key={h.id}
                      className={h.run_id ? "cursor-pointer hover:bg-panel-alt" : ""}
                      onClick={() => h.run_id && openRunDetail(h.run_id)}
                    >
                      <td>{fmtTime(h.timestamp)}</td>
                      <td>{h.symbol}</td>
                      <td>
                        <Pill text={h.action} />
                      </td>
                      <td className="whitespace-normal">{h.reasoning || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.agent_logs.length > 0 && (
            <div>
              <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-1.5">
                Agent-call hits ({result.agent_logs.length})
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Agent</th>
                    <th>Model</th>
                    <th>Summary</th>
                  </tr>
                </thead>
                <tbody>
                  {result.agent_logs.map((h) => (
                    <tr
                      key={h.id}
                      className={h.run_id ? "cursor-pointer hover:bg-panel-alt" : ""}
                      onClick={() => h.run_id && openRunDetail(h.run_id)}
                    >
                      <td>{fmtTime(h.timestamp)}</td>
                      <td>{h.agent_name}</td>
                      <td>{h.model || "—"}</td>
                      <td className="whitespace-normal">{h.output_summary || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
