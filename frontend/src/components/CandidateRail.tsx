import { Fragment, useMemo, useState } from "react";
import {
  Badge,
  Callout,
  Grid,
  Metric,
  Tab,
  TabGroup,
  TabList,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
  Text,
  TextInput,
  type Color,
} from "@tremor/react";
import {
  useLegacyTable as useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getExpandedRowModel,
  legacyCreateColumnHelper as createColumnHelper,
  type LegacyColumnDef,
} from "@tanstack/react-table/legacy";
import { flexRender, type SortingState, type ExpandedState } from "@tanstack/react-table";
import { CandidateFunnelItem, RunFunnelResponse } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";
import { useModalActions } from "../context/ModalContext";
import { fmtNum } from "../lib/format";
import { Stage, STAGE_META, candidateStage } from "./funnelShared";

const STAGE_COLOR: Record<Stage, Color> = {
  rejected: "slate",
  reached_pm: "cyan",
  proposed: "amber",
  risk_action: "rose",
  executed: "emerald",
};

const FILTERS: { key: Stage | "all"; label: string }[] = [
  { key: "all", label: "All" },
  { key: "reached_pm", label: "PM" },
  { key: "proposed", label: "Order" },
  { key: "risk_action", label: "Risk" },
  { key: "executed", label: "Exec" },
  { key: "rejected", label: "Screen" },
];

function DirGlyph({ direction }: { direction: CandidateFunnelItem["direction"] }) {
  const glyph = direction === "bullish" ? "▲" : direction === "bearish" ? "▼" : "•";
  const cls = direction === "bullish" ? "text-pos" : direction === "bearish" ? "text-neg" : "text-dim";
  return <span className={`${cls} w-3 shrink-0 text-center font-bold`}>{glyph}</span>;
}

interface Row {
  c: CandidateFunnelItem;
  stage: Stage;
}

function expandedSummary(c: CandidateFunnelItem, funnel: RunFunnelResponse): string {
  const parts: string[] = [];
  // Conviction is stated as risk since spec §2.1; legacy runs stated a
  // notional weight. Show whichever the PM actually emitted — labelled, since
  // "5% risk" and "5% of the book" are very different sizes.
  if (c.reached_pm_target && c.pm_risk_allocation_pct !== null) parts.push(`PM risk ${fmtNum(c.pm_risk_allocation_pct)}%`);
  else if (c.reached_pm_target && c.pm_target_weight_pct !== null) parts.push(`PM target ${fmtNum(c.pm_target_weight_pct)}%`);
  if (c.reached_proposed_order && c.proposed_action) parts.push(`Proposed ${c.proposed_action}`);
  if (c.risk_modified) parts.push("Modified by AI Risk Manager");
  if (c.executed && c.trade_action) parts.push(`Executed ${c.trade_action}`);
  if (!c.executed && c.execution_skip_reason) {
    parts.push(
      `Execution skipped — ${c.execution_skip_reason.replace(/_/g, " ")}${
        c.execution_skip_detail ? `: ${c.execution_skip_detail}` : ""
      }`
    );
  } else if (c.reached_proposed_order && funnel.hard_risk_block) {
    parts.push("Blocked by the deterministic hard-risk gate.");
  } else if (c.reached_proposed_order && funnel.risk_verdict?.verdict?.approved === false) {
    parts.push("Rejected by the AI Risk Manager.");
  } else if (c.reached_proposed_order && !c.executed) {
    parts.push("Not executed; execution reason was not recorded for this candidate.");
  } else if (c.reached_pm_target && !c.reached_proposed_order) {
    parts.push("No order was constructed; candidate-specific reason was not recorded.");
  }
  if (!parts.length) parts.push("Screened but never reached a Portfolio Manager target this run; candidate-specific reason was not recorded.");
  return parts.join(" · ");
}

const columnHelper = createColumnHelper<Row>();

export function CandidateRail({
  funnel,
  loading,
  error,
  updatedAt,
  selectedSymbol,
  onSelectSymbol,
}: {
  funnel: RunFunnelResponse | null;
  loading: boolean;
  error: string | null;
  updatedAt?: Date | null;
  selectedSymbol: string | null;
  onSelectSymbol: (symbol: string) => void;
}) {
  const { openCandidateDetail } = useModalActions();
  const [filter, setFilter] = useState<Stage | "all">("all");
  const [query, setQuery] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "stage", desc: false }]);
  const [expanded, setExpanded] = useState<ExpandedState>({});

  const buckets = useMemo(() => {
    const map: Record<Stage, CandidateFunnelItem[]> = {
      rejected: [],
      reached_pm: [],
      proposed: [],
      risk_action: [],
      executed: [],
    };
    if (funnel) for (const candidate of funnel.candidates) map[candidateStage(candidate, funnel)].push(candidate);
    return map;
  }, [funnel]);

  const total = funnel?.candidates.length ?? 0;
  const rows: Row[] = useMemo(() => {
    if (!funnel) return [];
    let list = filter === "all" ? funnel.candidates : buckets[filter];
    if (query.trim()) {
      const needle = query.trim().toUpperCase();
      list = list.filter((candidate) => candidate.symbol.includes(needle));
    }
    return list.map((candidate) => ({ c: candidate, stage: candidateStage(candidate, funnel) }));
  }, [funnel, buckets, filter, query]);

  const columns = useMemo(
    () => [
      columnHelper.accessor((row) => row.c.symbol, {
        id: "symbol",
        header: "Symbol",
        sortFn: "alphanumeric",
        cell: (info) => info.row.original.c.symbol,
      }),
      columnHelper.accessor((row) => STAGE_META[row.stage].rank, {
        id: "stage",
        header: "Stage",
        cell: (info) => STAGE_META[info.row.original.stage].short,
      }),
    ] as LegacyColumnDef<Row, unknown>[],
    []
  );

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting, expanded },
    onSortingChange: setSorting,
    onExpandedChange: setExpanded,
    getRowId: (row) => row.c.symbol,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
    getRowCanExpand: () => true,
  });

  const status = error ? (funnel ? "stale" : "error") : loading ? "loading" : "ok";
  const activeFilterIndex = FILTERS.findIndex((item) => item.key === filter);

  return (
    <Panel title="Candidates this run" subtitle={total > 0 ? `${total} shortlisted for consideration` : undefined} status={status} staleSince={updatedAt}>
      {error && !funnel && <StateMessage text={`Could not load candidates: ${error}`} error hero glyph="■" />}
      {!error && !funnel && !loading && (
        <StateMessage text="No session yet today. Candidates populate once QAMC's first scan completes." hero glyph="○" />
      )}
      {!error && !funnel && loading && <StateMessage text="Loading…" hero />}
      {funnel && total === 0 && <StateMessage text="No candidates considered in the selected run." />}
      {funnel && total > 0 && (
        <div className="flex min-h-0 flex-col gap-3">
          {error && (
            <Callout title="Last known candidate set" color="amber" className="!bg-panel-alt">
              As of {updatedAt ? updatedAt.toLocaleTimeString() : "an earlier fetch"}; fresh fetch failed ({error}).
            </Callout>
          )}

          <Grid numItems={2} className="gap-px overflow-hidden rounded-lg bg-border ring-1 ring-border">
            {[
              ["Considered", funnel.candidates_considered],
              ["Reached PM", funnel.reached_pm_count],
              ["Proposed", funnel.proposed_order_count],
              ["Executed", funnel.executed_count],
            ].map(([label, value]) => (
              <div key={label} className="flex min-w-0 items-center justify-between bg-panel-alt px-3 py-2">
                <Text className="text-xs uppercase tracking-wide">{label}</Text>
                <Metric className="font-mono text-lg text-ink">{value}</Metric>
              </div>
            ))}
          </Grid>

          <TabGroup index={activeFilterIndex} onIndexChange={(index) => setFilter(FILTERS[index].key)}>
            <TabList variant="solid" color="cyan" className="max-w-full overflow-x-auto bg-panel-alt p-1 ring-1 ring-border">
              {FILTERS.map((item) => {
                const count = item.key === "all" ? total : buckets[item.key].length;
                return (
                  <Tab key={item.key} className="gap-1 whitespace-nowrap px-2 py-1.5 text-xs">
                    {item.label} <span className="font-mono text-dim">{count}</span>
                  </Tab>
                );
              })}
            </TabList>
          </TabGroup>

          <TextInput value={query} onValueChange={setQuery} placeholder="Filter symbol…" className="!bg-panel-alt !ring-border" />

          {funnel.bearish_hedge_considered && (
            <Badge color="fuchsia" size="xs">Bearish-hedge candidate in this run</Badge>
          )}

          <div className="min-h-0 overflow-y-auto rounded-lg ring-1 ring-border">
            {rows.length === 0 ? (
              <StateMessage text="No candidates match this filter." />
            ) : (
              <Table className="text-sm">
                <TableHead>
                  <TableRow>
                    {table.getHeaderGroups()[0].headers.map((header) => (
                      <TableHeaderCell key={header.id} className={header.id === "stage" ? "w-20" : ""}>
                        <button type="button" onClick={header.column.getToggleSortingHandler()} className="uppercase tracking-wide">
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          {{ asc: " ↑", desc: " ↓" }[header.column.getIsSorted() as string] ?? ""}
                        </button>
                      </TableHeaderCell>
                    ))}
                  </TableRow>
                </TableHead>
                <TableBody>
                  {table.getRowModel().rows.map((row) => {
                    const candidate = row.original.c;
                    const stage = row.original.stage;
                    const active = candidate.symbol === selectedSymbol;
                    return (
                      <Fragment key={row.id}>
                        <TableRow className={active ? "bg-cyan-500/10" : "hover:bg-panel-alt"}>
                          <TableCell className="!px-2 !py-2">
                            <div className="flex items-center gap-1.5">
                              <button
                                type="button"
                                onClick={row.getToggleExpandedHandler()}
                                className="w-4 shrink-0 text-dim hover:text-ink"
                                aria-label={row.getIsExpanded() ? "Collapse" : "Expand"}
                              >
                                {row.getIsExpanded() ? "▾" : "▸"}
                              </button>
                              <button
                                type="button"
                                className="flex min-w-0 items-center gap-1.5 text-left font-semibold"
                                onClick={() => {
                                  onSelectSymbol(candidate.symbol);
                                  openCandidateDetail(funnel.run_id, candidate.symbol);
                                }}
                              >
                                <DirGlyph direction={candidate.direction} />
                                {candidate.symbol}
                                {candidate.is_bearish_hedge && <span className="text-[0.65rem] text-hedge">HEDGE</span>}
                              </button>
                            </div>
                          </TableCell>
                          <TableCell className="!px-2 !py-2 text-right">
                            <Badge color={STAGE_COLOR[stage]} size="xs">{STAGE_META[stage].short}</Badge>
                          </TableCell>
                        </TableRow>
                        {row.getIsExpanded() && (
                          <TableRow className="bg-panel-alt/70">
                            <TableCell colSpan={2} className="!whitespace-normal !px-3 !py-2 text-xs leading-snug text-dim">
                              {expandedSummary(candidate, funnel)}
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </div>

          {buckets.risk_action.length > 0 && (
            <Text className="text-xs leading-snug">
              Risk-verdict attribution is per run, not per candidate; a rejected or blocked run attributes to every proposed candidate within it.
            </Text>
          )}
        </div>
      )}
    </Panel>
  );
}
