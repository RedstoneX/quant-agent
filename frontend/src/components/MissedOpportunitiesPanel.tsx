import { useEffect, useState } from "react";
import type { EChartsOption } from "echarts";
import { api } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";
import { EChart } from "./ui/EChart";
import { readQamcTheme } from "../lib/theme";
import { fmtPct } from "../lib/format";

// Same six categories evening_analyst.py's MissedOpportunity model emits
// (src/models.py). "noise_rally"/"risk_disciplined" are the model's own
// escape hatches for "not actually a miss" — grouped separately (dim) from
// the four genuine-miss categories (warn) so the chart never implies the
// system regrets a disciplined pass.
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

// A real longitudinal shape (move size/direction over time, genuine-miss vs
// disciplined-pass) is more useful than a single day's bullet list for the
// product's own directional-neutrality question ("what did it miss, in
// which direction, and is that a pattern?" — docs/OUTCOME.md) — so this
// aggregates the last N journal days client-side from the same per-day
// endpoint the old single-day view used, rather than adding a backend
// aggregation route for a read the frontend can already assemble.
function MissedOpportunitiesScatter({ entries }: { entries: MissedEntry[] }) {
  const theme = readQamcTheme();
  const dates = Array.from(new Set(entries.map((e) => e.date))).sort();

  const option: EChartsOption = {
    grid: { left: 44, right: 16, top: 16, bottom: 34 },
    xAxis: {
      type: "category",
      data: dates,
      axisLine: { lineStyle: { color: theme.border } },
      axisLabel: { color: theme.inkDim, fontFamily: "'IBM Plex Mono', monospace", fontSize: 9, formatter: (v: string) => v.slice(5) },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      name: "Move %",
      nameTextStyle: { color: theme.inkDim, fontSize: 9 },
      axisLabel: { color: theme.inkDim, fontFamily: "'IBM Plex Mono', monospace", fontSize: 9, formatter: "{value}%" },
      splitLine: { lineStyle: { color: theme.border, type: "dashed" } },
    },
    tooltip: {
      formatter: (params: unknown) => {
        const p = params as { data: MissedEntry & { value: [string, number] } };
        const d = p.data;
        const tag = NOT_A_MISS.has(d.miss_category) ? "not a real miss" : d.miss_category.replace(/_/g, " ");
        return `<b>${d.symbol}</b> · ${d.date}<br/>${fmtPct(d.move_pct)} · ${tag}${d.lesson ? `<br/><span style="opacity:0.75">${d.lesson}</span>` : ""}`;
      },
    },
    series: [
      {
        type: "scatter",
        symbolSize: 9,
        data: entries.map((e) => ({
          ...e,
          value: [e.date, e.move_pct],
          itemStyle: { color: NOT_A_MISS.has(e.miss_category) ? theme.inkDim : theme.amber },
        })),
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: theme.border, type: "solid", width: 1 },
          label: { show: false },
          data: [{ yAxis: 0 }],
        },
      },
    ],
  };

  return <EChart option={option} height={190} />;
}

export function MissedOpportunitiesPanel() {
  const [entries, setEntries] = useState<MissedEntry[] | null>(null);
  const [latestDate, setLatestDate] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .journalDates(HISTORY_DAYS)
      .then((res) => {
        if (cancelled || !res.dates.length) {
          if (!cancelled) setLoading(false);
          return;
        }
        const latest = res.dates[0];
        return Promise.all(
          res.dates.map((d) => api.journalDay(d).then((day) => parseMissed(d, day.reflection?.missed_opportunities_json ?? null)))
        ).then((perDay) => {
          if (cancelled) return;
          setEntries(perDay.flat());
          setLatestDate(latest);
        });
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const latestMissed = entries && latestDate ? entries.filter((e) => e.date === latestDate) : [];
  const status = error ? "error" : loading ? "loading" : "ok";
  const realMissCount = entries?.filter((e) => !NOT_A_MISS.has(e.miss_category)).length ?? 0;

  return (
    <Panel
      title="Missed opportunities"
      subtitle={`Notable moves the evening review flagged — up or down, last ${HISTORY_DAYS} days.`}
      status={status}
    >
      {error && <StateMessage text={`Could not load missed opportunities: ${error}`} error />}
      {!error && !loading && (!entries || entries.length === 0) && (
        <StateMessage text="No missed opportunities recorded in the available journal history." />
      )}
      {!error && entries && entries.length > 0 && (
        <div>
          <div className="text-dim text-[0.68rem] uppercase tracking-wide mb-1.5">
            {realMissCount} genuine miss{realMissCount === 1 ? "" : "es"} · {entries.length - realMissCount} disciplined pass
            {entries.length - realMissCount === 1 ? "" : "es"}
          </div>
          <MissedOpportunitiesScatter entries={entries} />
          {latestDate && latestMissed.length > 0 && (
            <div className="mt-2.5">
              <div className="text-dim text-[0.78rem] mb-1.5">From {latestDate} evening review:</div>
              <ul className="pl-4 text-[0.79rem] list-disc">
                {latestMissed.map((item, i) => (
                  <li key={i}>
                    {item.symbol} · {item.miss_category.replace(/_/g, " ")} · {fmtPct(item.move_pct)}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
