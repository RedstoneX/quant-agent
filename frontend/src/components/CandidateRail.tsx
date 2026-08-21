import { useMemo, useState } from "react";
// TanStack Table v9 ships a first-class `/legacy` compatibility export
// (package.json `exports["./legacy"]`) providing the familiar v8-style
// `useReactTable`-shaped API (`useLegacyTable`/`legacyCreateColumnHelper`)
// on top of the new v9 core — genuinely the same maintained library, not a
// workaround, chosen over hand-adopting v9's new atoms/store-based
// `useTable` API for this dense sort+expand use case.
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
import { EChart } from "./ui/EChart";
import { readQamcTheme } from "../lib/theme";
import { useModalActions } from "../context/ModalContext";
import { fmtNum } from "../lib/format";
import { Stage, STAGE_META, STAGE_ORDER, candidateStage } from "./funnelShared";

/* Left rail: the candidate universe collapsed into the funnel stage each
 * candidate actually reached, instead of one flat "PM: no proposal" pill
 * per ticker (the prior WatchlistPanel dumped all 80+ candidates as an
 * unbounded page-length list). Each candidate is bucketed into exactly
 * one stage — the FURTHEST stage it reached — so the five buckets below
 * partition the full candidate set with no double-counting.
 *
 * The row list itself is a real TanStack Table instance (headless — QAMC
 * owns 100% of the markup/styling) rather than a plain mapped array: real
 * column sorting (by symbol or by furthest stage reached) and expandable
 * rows (an inline PM/Risk summary from data already on the row, no extra
 * fetch) is a genuine watchlist-grid upgrade a hand-rolled list wouldn't
 * get for free. */

function DirGlyph({ direction }: { direction: CandidateFunnelItem["direction"] }) {
  const glyph = direction === "bullish" ? "▲" : direction === "bearish" ? "▼" : "•";
  const cls = direction === "bullish" ? "text-pos" : direction === "bearish" ? "text-neg" : "text-dim";
  return <span className={`${cls} font-bold w-3 inline-block text-center flex-shrink-0`}>{glyph}</span>;
}

// The funnel (Considered -> Reached PM -> Proposed -> Executed) as a real
// ECharts funnel series — connected, narrowing, not just four bars. This
// aggregate genuinely IS linear/narrowing (each count is a subset of the
// one before it), unlike the 5-way bucket split below it, which branches
// (rejected/risk-modified are exits, not further narrowing) and is left as
// the bucket chips rather than force-fit into the same funnel shape.
function CandidateFunnelChart({ funnel }: { funnel: RunFunnelResponse }) {
  const theme = readQamcTheme();
  const data = [
    { name: "Considered", value: funnel.candidates_considered },
    { name: "Reached PM", value: funnel.reached_pm_count },
    { name: "Proposed", value: funnel.proposed_order_count },
    { name: "Executed", value: funnel.executed_count },
  ];
  const option = {
    tooltip: { trigger: "item" as const, formatter: "{b}: {c}" },
    series: [
      {
        type: "funnel" as const,
        left: 16,
        right: 16,
        top: 6,
        bottom: 6,
        // 58%, not the default value-proportional shrink to a 30% floor —
        // "Reached PM"/"Proposed"/"Executed" counts are typically tiny
        // relative to "Considered" (95 vs. 5), so a strictly value-scaled
        // funnel clamped the three narrowest bands to the smallest usable
        // width, clipping their own count label against the trapezoid
        // edge (e.g. "Reached PM 5" losing its "5"). A higher floor keeps
        // the funnel SHAPE (still visibly narrowing) while guaranteeing
        // every label has room to render in full.
        minSize: "58%",
        maxSize: "100%",
        gap: 4,
        sort: "none" as const,
        label: {
          position: "inside" as const,
          formatter: "{b}  {c}",
          color: theme.bg,
          fontFamily: "'IBM Plex Sans', sans-serif",
          fontWeight: 700,
          fontSize: 11.5,
        },
        itemStyle: {
          borderColor: theme.panelInset,
          borderWidth: 2,
        },
        color: [theme.accent, theme.accent, theme.amber, theme.green],
        data,
      },
    ],
  };
  return <EChart option={option} height={150} />;
}

function StageChip({
  active,
  label,
  count,
  onClick,
  dotClass,
}: {
  active: boolean;
  label: string;
  count: number;
  onClick: () => void;
  dotClass: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1.5 px-2 py-1.5 rounded-md border text-left ${
        active ? "border-accent bg-accent/10" : "border-border bg-panel-alt hover:border-accent/50"
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dotClass}`} />
      <span className="text-[0.7rem] uppercase tracking-wide text-dim leading-tight flex-1">{label}</span>
      <span className="text-[0.875rem] font-extrabold tabular-nums flex-shrink-0">{count}</span>
    </button>
  );
}

interface Row {
  c: CandidateFunnelItem;
  stage: Stage;
}

function ExpandedSummary({ c }: { c: CandidateFunnelItem }) {
  const parts: string[] = [];
  if (c.reached_pm_target && c.pm_target_weight_pct !== null) parts.push(`PM target ${fmtNum(c.pm_target_weight_pct)}%`);
  if (c.reached_proposed_order && c.proposed_action) parts.push(`Proposed ${c.proposed_action}`);
  if (c.risk_modified) parts.push("Modified by AI Risk Manager");
  if (c.executed && c.trade_action) parts.push(`Executed ${c.trade_action}`);
  if (!parts.length) parts.push("Screened but never reached a Portfolio Manager target this run.");
  return <div className="px-2 pb-1.5 pt-0.5 text-[0.7rem] text-dim leading-snug">{parts.join(" · ")}</div>;
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
  const [q, setQ] = useState("");
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
    if (funnel) {
      for (const c of funnel.candidates) map[candidateStage(c, funnel)].push(c);
    }
    return map;
  }, [funnel]);

  const total = funnel?.candidates.length ?? 0;

  const rows: Row[] = useMemo(() => {
    if (!funnel) return [];
    let list = filter === "all" ? funnel.candidates : buckets[filter];
    if (q.trim()) {
      const needle = q.trim().toUpperCase();
      list = list.filter((c) => c.symbol.includes(needle));
    }
    return list.map((c) => ({ c, stage: candidateStage(c, funnel) }));
  }, [funnel, buckets, filter, q]);

  const columns = useMemo(
    () => [
      columnHelper.accessor((r) => r.c.symbol, {
        id: "symbol",
        header: "Symbol",
        sortFn: "alphanumeric",
        cell: (info) => {
          const c = info.row.original.c;
          return (
            <span className="flex items-center gap-1.5">
              <DirGlyph direction={c.direction} />
              <span className="font-semibold">{c.symbol}</span>
              {c.is_bearish_hedge && <span className="text-hedge text-[0.7rem] font-bold uppercase">hedge</span>}
            </span>
          );
        },
      }),
      columnHelper.accessor((r) => STAGE_META[r.stage].rank, {
        id: "stage",
        header: "Stage",
        cell: (info) => {
          const meta = STAGE_META[info.row.original.stage];
          return <span className={`text-[0.7rem] font-semibold uppercase tracking-wide ${meta.textClass}`}>{meta.short}</span>;
        },
      }),
      // Heterogeneous accessor return types (string vs number) make the
      // array-literal element type union awkwardly across TanStack Table's
      // generic ColumnDef — an explicit cast here is a pure type-inference
      // workaround, not a runtime behavior change.
    ] as LegacyColumnDef<Row, unknown>[],
    []
  );

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting, expanded },
    onSortingChange: setSorting,
    onExpandedChange: setExpanded,
    getRowId: (r) => r.c.symbol,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
    getRowCanExpand: () => true,
  });

  // Same stale-not-blank contract as DecisionRoomPanel: a failed poll keeps
  // rendering the last-loaded candidate set, tagged stale, instead of
  // silently discarding real data or presenting it as if it were current.
  const status = error ? (funnel ? "stale" : "error") : loading ? "loading" : "ok";

  return (
    <Panel
      title="Candidates this run"
      subtitle={funnel && total > 0 ? `${total} shortlisted for consideration` : undefined}
      status={status}
      staleSince={updatedAt}
    >
      {error && !funnel && <StateMessage text={`Could not load candidates: ${error}`} error hero glyph="■" />}
      {!error && !funnel && !loading && (
        <StateMessage
          text="No session yet today. Candidates populate once QAMC's first scan completes."
          hero
          glyph="○"
        />
      )}
      {!error && !funnel && loading && <StateMessage text="Loading…" hero />}
      {funnel && total === 0 && <StateMessage text="No candidates considered in the latest run." />}
      {funnel && total > 0 && (
        <div>
          {error && (
            <div className="text-warn text-[0.8125rem] bg-warn/10 border border-warn/30 rounded-md px-2 py-1.5 mb-2.5">
              Stale — last known as of {updatedAt ? updatedAt.toLocaleTimeString() : "an earlier fetch"} ({error})
            </div>
          )}
          <CandidateFunnelChart funnel={funnel} />
          {funnel.bearish_hedge_considered && (
            <div className="inline-flex items-center gap-1.5 mb-2.5 mt-1 px-2 py-1 rounded-md bg-hedge/10 border border-hedge/30">
              <span className="w-1.5 h-1.5 rounded-full bg-hedge flex-shrink-0" />
              <span className="text-hedge text-[0.7rem] font-bold uppercase tracking-wide">Bearish-hedge candidate in this run</span>
            </div>
          )}
          <div className="grid grid-cols-2 gap-1.5 mb-2.5">
            <StageChip active={filter === "all"} label="Shortlisted" count={total} onClick={() => setFilter("all")} dotClass="bg-accent" />
            {STAGE_ORDER.slice()
              .reverse()
              .map((s) => (
                <StageChip
                  key={s}
                  active={filter === s}
                  label={STAGE_META[s].label}
                  count={buckets[s].length}
                  onClick={() => setFilter(filter === s ? "all" : s)}
                  dotClass={STAGE_META[s].dotClass}
                />
              ))}
          </div>

          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter symbol…"
            className="w-full bg-panel-alt border border-border rounded text-[0.8125rem] px-2 py-1 mb-2"
          />

          {/* Column headers double as sort toggles — real multi-key sort
              (symbol / furthest stage reached) a hand-rolled list wouldn't
              get for free. */}
          <div className="flex items-center gap-2 px-2 pb-1 text-[0.7rem] text-dim uppercase tracking-wide font-semibold">
            {table.getHeaderGroups()[0].headers.map((h) => (
              <button
                key={h.id}
                type="button"
                onClick={h.column.getToggleSortingHandler()}
                className={`hover:text-ink ${h.id === "symbol" ? "flex-1 text-left" : "w-14 text-left"}`}
              >
                {flexRender(h.column.columnDef.header, h.getContext())}
                {{ asc: " ▲", desc: " ▼" }[h.column.getIsSorted() as string] ?? ""}
              </button>
            ))}
          </div>

          <div className="flex flex-col gap-0.5 max-h-[420px] overflow-y-auto -mx-1 px-1">
            {rows.length === 0 && <StateMessage text="No candidates match this filter." />}
            {table.getRowModel().rows.map((row) => {
              const c = row.original.c;
              const active = c.symbol === selectedSymbol;
              return (
                <div key={row.id}>
                  <div
                    className={`flex items-center gap-1.5 px-2 py-1.5 rounded-md text-left text-[0.875rem] border ${
                      active ? "border-accent bg-accent/10" : "border-transparent hover:bg-panel-alt"
                    }`}
                  >
                    <button
                      type="button"
                      onClick={row.getToggleExpandedHandler()}
                      className="text-dim hover:text-ink flex-shrink-0 w-3.5 text-center"
                      aria-label={row.getIsExpanded() ? "Collapse" : "Expand"}
                    >
                      {row.getIsExpanded() ? "▾" : "▸"}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        onSelectSymbol(c.symbol);
                        openCandidateDetail(funnel.run_id, c.symbol);
                      }}
                      className="flex items-center gap-2 flex-1 min-w-0 text-left"
                    >
                      {flexRender(row.getVisibleCells()[0].column.columnDef.cell, row.getVisibleCells()[0].getContext())}
                      <span className="ml-auto flex-shrink-0">
                        {flexRender(row.getVisibleCells()[1].column.columnDef.cell, row.getVisibleCells()[1].getContext())}
                      </span>
                    </button>
                  </div>
                  {row.getIsExpanded() && <ExpandedSummary c={c} />}
                </div>
              );
            })}
          </div>

          {buckets.risk_action.length > 0 && (
            <div className="state-message mt-2 text-[0.7rem]">
              Risk-verdict attribution is per run, not per candidate — a rejected/blocked run attributes to every
              proposed candidate within it.
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
