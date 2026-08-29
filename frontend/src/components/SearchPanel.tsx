import { useMemo, useState } from "react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { api, SearchAgentLogHit, SearchResponse, SearchTradeHit } from "../api/client";
import { fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { useModalActions } from "../context/ModalContext";
import { DataTable } from "./ui/DataTable";

const tradeColumn = createColumnHelper<SearchTradeHit>();
const agentColumn = createColumnHelper<SearchAgentLogHit>();

// Trailing, always-on (not hover-only) hint that a row opens Run Detail —
// added for both hit tables below alongside DataTable's existing
// cursor-pointer/hover-bg row styling (already applied automatically to
// any onRowClick table; see PositionsPanel/OrdersPanel/RunsPanel), because
// that styling alone only shows up on hover, and plain cells like
// Reasoning/Summary gave no hint at rest that the row underneath them was
// clickable. Reuses this file's own "uppercase tracking-wide text-dim"
// label idiom (see the "Trade hits (n)"/"Agent-call hits (n)" headers
// below) rather than inventing new styling.
function OpensRunHint() {
  return <span className="text-[0.75rem] uppercase tracking-wide text-dim whitespace-nowrap">Run &rarr;</span>;
}

export function SearchPanel({ onSelectSymbol }: { onSelectSymbol?: (symbol: string) => void }) {
  const [q, setQ] = useState("");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const { openRunDetail } = useModalActions();
  const tradeColumns = useMemo(() => [
    tradeColumn.accessor("timestamp", { header: "Time", cell: (info) => fmtTime(info.getValue()) }),
    // Symbol-cell-specific click: charts the symbol in place, modal-free,
    // same as PositionsPanel's/OrdersPanel's/TradesPanel's onSelectSymbol
    // — deliberately separate from the row's onRowClick below (which
    // opens this hit's Run Detail). A hit's symbol answers "what is
    // this?"; the rest of the row answers "what run produced it?" — two
    // different questions, so a click on the symbol must not fire the
    // row's modal too (see the stopPropagation below, same idiom as
    // OrdersPanel/TradesPanel's symbol cell).
    tradeColumn.accessor("symbol", {
      header: "Symbol",
      cell: (info) =>
        onSelectSymbol ? (
          <button
            type="button"
            className="font-bold text-accent hover:underline"
            onClick={(event) => {
              event.stopPropagation();
              onSelectSymbol(info.getValue());
            }}
          >
            {info.getValue()}
          </button>
        ) : (
          <span className="font-bold text-accent">{info.getValue()}</span>
        ),
    }),
    tradeColumn.accessor("action", { header: "Action", cell: (info) => <Pill text={info.getValue()} /> }),
    tradeColumn.accessor("reasoning", { header: "Reasoning", cell: (info) => <span className="block max-w-[36rem] whitespace-normal font-sans">{info.getValue() || "—"}</span> }),
    tradeColumn.display({ id: "opensRun", header: "", cell: () => <OpensRunHint /> }),
  ] as LegacyColumnDef<SearchTradeHit, unknown>[], [onSelectSymbol]);
  const agentColumns = useMemo(() => [
    agentColumn.accessor("timestamp", { header: "Time", cell: (info) => fmtTime(info.getValue()) }),
    // No symbol column exists on this table — an agent-call hit's only
    // meaningful target is the run it belongs to, so (unlike the trade
    // hits above) the whole row keeps a single, undivided click straight
    // to Run Detail. Bold/accent-colored, same idiom Positions/Orders/
    // Trades/RunsPanel use to mark the identifying cell of a clickable
    // row.
    agentColumn.accessor("agent_name", { header: "Agent", cell: (info) => <span className="font-bold text-accent">{info.getValue()}</span> }),
    agentColumn.accessor("model", { header: "Model", cell: (info) => info.getValue() || "—" }),
    agentColumn.accessor("output_summary", { header: "Summary", cell: (info) => <span className="block max-w-[36rem] whitespace-normal font-sans">{info.getValue() || "—"}</span> }),
    agentColumn.display({ id: "opensRun", header: "", cell: () => <OpensRunHint /> }),
  ] as LegacyColumnDef<SearchAgentLogHit, unknown>[], []);

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
              <DataTable data={result.trades} columns={tradeColumns} getRowId={(hit) => String(hit.id)} onRowClick={(hit) => hit.run_id && openRunDetail(hit.run_id)} />
            </div>
          )}
          {result.agent_logs.length > 0 && (
            <div>
              <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-1.5">
                Agent-call hits ({result.agent_logs.length})
              </div>
              <DataTable data={result.agent_logs} columns={agentColumns} getRowId={(hit) => String(hit.id)} onRowClick={(hit) => hit.run_id && openRunDetail(hit.run_id)} />
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
