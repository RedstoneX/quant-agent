import { useEffect, useMemo, useState } from "react";
import { Card, Grid, Metric, Text, Badge } from "@tremor/react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { api } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";
import { DataTable } from "./ui/DataTable";
import { fmtPct } from "../lib/format";

const NOT_A_MISS = new Set(["noise_rally", "risk_disciplined"]);
const HISTORY_DAYS = 20;

interface MissedEntry {
  date: string;
  symbol: string;
  move_pct: number;
  miss_category: string;
  lesson: string;
}

function parseMissed(date: string, json: string | null): MissedEntry[] {
  if (!json) return [];
  try {
    const data = JSON.parse(json);
    if (!Array.isArray(data)) return [];
    return data
      .filter((item) => item && typeof item.symbol === "string" && typeof item.move_pct === "number")
      .map((item) => ({
        date,
        symbol: item.symbol,
        move_pct: item.move_pct,
        miss_category: typeof item.miss_category === "string" ? item.miss_category : "unknown",
        lesson: typeof item.lesson === "string" ? item.lesson : "",
      }));
  } catch {
    return [];
  }
}

const columnHelper = createColumnHelper<MissedEntry>();

export function MissedOpportunitiesPanel({ onSelectSymbol }: { onSelectSymbol?: (symbol: string) => void }) {
  const [entries, setEntries] = useState<MissedEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api.journalDates(HISTORY_DAYS)
      .then((res) => Promise.all(res.dates.map((date) => api.journalDay(date).then((day) => parseMissed(date, day.reflection?.missed_opportunities_json ?? null)))))
      .then((perDay) => {
        if (!cancelled) setEntries(perDay.flat());
      })
      .catch((reason) => {
        if (!cancelled) setError(reason.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const columns = useMemo(
    () => [
      columnHelper.accessor("date", { header: "Date" }),
      columnHelper.accessor("symbol", { header: "Symbol", cell: (info) => <span className="font-bold text-accent">{info.getValue()}</span> }),
      columnHelper.accessor("move_pct", { header: "Move", cell: (info) => <span className={info.getValue() >= 0 ? "text-pos" : "text-neg"}>{fmtPct(info.getValue())}</span> }),
      columnHelper.accessor((entry) => NOT_A_MISS.has(entry.miss_category) ? "Disciplined pass" : "Genuine miss", {
        id: "assessment",
        header: "Assessment",
        cell: (info) => <Badge color={info.getValue() === "Genuine miss" ? "amber" : "slate"} size="xs">{info.getValue()}</Badge>,
      }),
      columnHelper.accessor("miss_category", { header: "Category", cell: (info) => info.getValue().replace(/_/g, " ") }),
      columnHelper.accessor("lesson", { header: "Lesson", cell: (info) => <span className="block max-w-[34rem] whitespace-normal font-sans">{info.getValue() || "—"}</span> }),
    ] as LegacyColumnDef<MissedEntry, unknown>[],
    []
  );

  const realMissCount = entries?.filter((entry) => !NOT_A_MISS.has(entry.miss_category)).length ?? 0;
  const disciplinedCount = (entries?.length ?? 0) - realMissCount;
  const status = error ? "error" : loading ? "loading" : "ok";

  return (
    <Panel title="Missed opportunities" subtitle={`Evening-review lessons across the latest ${HISTORY_DAYS} journal days.`} status={status}>
      {error && <StateMessage text={`Could not load missed opportunities: ${error}`} error />}
      {!error && !loading && (!entries || entries.length === 0) && <StateMessage text="No missed opportunities recorded in the available journal history." />}
      {!error && entries && entries.length > 0 && (
        <div className="space-y-3">
          <Grid numItems={2} className="gap-2.5">
            <Card className="!bg-panel-alt !p-3 !ring-border"><Text>Genuine misses</Text><Metric className="font-mono text-xl text-warn">{realMissCount}</Metric></Card>
            <Card className="!bg-panel-alt !p-3 !ring-border"><Text>Disciplined passes</Text><Metric className="font-mono text-xl text-ink">{disciplinedCount}</Metric></Card>
          </Grid>
          <DataTable
            data={entries}
            columns={columns}
            getRowId={(entry) => `${entry.date}:${entry.symbol}:${entry.miss_category}`}
            initialSorting={[{ id: "date", desc: true }]}
            onRowClick={onSelectSymbol ? (entry) => onSelectSymbol(entry.symbol) : undefined}
          />
        </div>
      )}
    </Panel>
  );
}
