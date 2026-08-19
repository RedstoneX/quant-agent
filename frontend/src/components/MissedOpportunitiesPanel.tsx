import { useEffect, useState } from "react";
import { api, ReflectionItem } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";

function parseMissed(json: string | null): Record<string, unknown>[] | null {
  if (!json) return null;
  try {
    const data = JSON.parse(json);
    return Array.isArray(data) && data.length ? data : null;
  } catch {
    return null;
  }
}

export function MissedOpportunitiesPanel() {
  const [date, setDate] = useState<string | null>(null);
  const [reflection, setReflection] = useState<ReflectionItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .journalDates(1)
      .then((dates) => {
        if (cancelled) return;
        if (!dates.dates.length) {
          setLoading(false);
          return;
        }
        const d = dates.dates[0];
        setDate(d);
        return api.journalDay(d).then((day) => {
          if (!cancelled) setReflection(day.reflection);
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

  const status = error ? "error" : loading ? "loading" : "ok";
  const missed = parseMissed(reflection?.missed_opportunities_json ?? null);

  return (
    <Panel title="Missed opportunities" subtitle="Notable moves the evening review flagged — up or down." status={status}>
      {error && <StateMessage text={`Could not load missed opportunities: ${error}`} error />}
      {!error && !loading && !date && <StateMessage text="No journal data recorded yet." />}
      {!error && !loading && date && !missed && (
        <StateMessage text={`No missed opportunities recorded in the ${date} evening review.`} />
      )}
      {!error && missed && (
        <div>
          <div className="text-dim text-[0.78rem] mb-1.5">From {date} evening review:</div>
          <ul className="pl-4 text-[0.79rem] list-disc">
            {missed.map((item, i) => {
              const parts = ["symbol", "miss_category", "move_pct"]
                .filter((f) => item[f] !== undefined && item[f] !== null && item[f] !== "")
                .map((f) => `${f.replace(/_/g, " ")}: ${item[f]}`);
              return <li key={i}>{parts.length ? parts.join(" · ") : JSON.stringify(item)}</li>;
            })}
          </ul>
        </div>
      )}
    </Panel>
  );
}
