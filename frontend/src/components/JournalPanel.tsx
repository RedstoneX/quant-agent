import { useEffect, useState } from "react";
import { api, JournalDayResponse, RunSummary } from "../api/client";
import { fmtMoney, fmtNum, fmtPct, fmtTime, pnlClass } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { Card, CardText, EvidenceSection } from "./ui/Evidence";
import { useModalActions } from "../context/ModalContext";

function parseBlobList(json: string | null, fields: string[]): { text: string }[] | null {
  if (!json) return null;
  try {
    const data = JSON.parse(json);
    if (!Array.isArray(data) || !data.length) return null;
    return data.map((item: Record<string, unknown>) => {
      const parts = fields
        .filter((f) => item[f] !== undefined && item[f] !== null && item[f] !== "")
        .map((f) => `${f.replace(/_/g, " ")}: ${item[f]}`);
      return { text: parts.length ? parts.join(" · ") : JSON.stringify(item) };
    });
  } catch {
    return null;
  }
}

function ReflectionCard({ reflection }: { reflection: JournalDayResponse["reflection"] }) {
  if (!reflection) return <StateMessage text="No evening reflection recorded for this day yet." />;
  const missed = parseBlobList(reflection.missed_opportunities_json, ["symbol", "miss_category", "move_pct"]);
  return (
    <Card title="Evening reflection">
      {reflection.tomorrow_outlook && <CardText text={`Tomorrow outlook: ${reflection.tomorrow_outlook}`} />}
      {reflection.lessons && <CardText text={`Lessons: ${reflection.lessons}`} />}
      {reflection.tomorrow_bias && (
        <div className="kv-row">
          <span className="text-dim">Tomorrow bias</span>
          <span>{reflection.tomorrow_bias}</span>
        </div>
      )}
      {missed && (
        <>
          <div className="text-[0.65rem] text-dim uppercase tracking-wide font-semibold mt-2">Missed opportunities</div>
          <ul className="pl-4 text-[0.79rem] list-disc mt-1">
            {missed.map((m, i) => (
              <li key={i}>{m.text}</li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

export function JournalPanel({ onOpenCandidate }: { onOpenCandidate: (runs: RunSummary[], symbol: string) => void }) {
  const [dates, setDates] = useState<string[]>([]);
  const [date, setDate] = useState<string>("");
  const [day, setDay] = useState<JournalDayResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { openRunDetail } = useModalActions();

  useEffect(() => {
    let cancelled = false;
    api
      .journalDates(60)
      .then((d) => {
        if (cancelled) return;
        setDates(d.dates);
        if (d.dates.length) setDate(d.dates[0]);
        else setLoading(false);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!date) return;
    let cancelled = false;
    setLoading(true);
    api
      .journalDay(date)
      .then((d) => {
        if (!cancelled) setDay(d);
      })
      .catch((err) => {
        if (!cancelled) setError(err.status === 404 ? null : err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [date]);

  const status = error ? "error" : loading ? "loading" : "ok";

  return (
    <Panel
      title="Journal"
      subtitle="Prior trading day: equity snapshot, evening reflection, runs, trades, candidates."
      status={status}
      full
      actions={
        dates.length > 0 && (
          <select
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="bg-panel-alt border border-border rounded text-[0.78rem] px-1.5 py-0.5"
          >
            {dates.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        )
      }
    >
      {error && <StateMessage text={`Could not load journal: ${error}`} error />}
      {!error && dates.length === 0 && !loading && <StateMessage text="No journal data recorded yet." />}
      {!error && day && (
        <div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 mb-3">
            <div>
              <div className="text-[0.68rem] text-dim uppercase tracking-wide">Equity close</div>
              <div className="text-[1.1rem] font-bold tabular-nums">
                {day.daily_pnl ? fmtMoney(day.daily_pnl.equity_close) : "—"}
              </div>
            </div>
            <div>
              <div className="text-[0.68rem] text-dim uppercase tracking-wide">Daily P&L</div>
              <div className={`text-[1.1rem] font-bold tabular-nums ${day.daily_pnl ? pnlClass(day.daily_pnl.daily_pnl) : ""}`}>
                {day.daily_pnl
                  ? `${fmtMoney(day.daily_pnl.daily_pnl)} (${fmtPct(day.daily_pnl.daily_return_pct)})`
                  : "—"}
              </div>
            </div>
          </div>

          <EvidenceSection title="Evening reflection">{[<ReflectionCard key="r" reflection={day.reflection} />]}</EvidenceSection>

          <EvidenceSection title="Runs this day" emptyText="No runs recorded for this day.">
            {day.runs.length
              ? [
                  <table key="runs">
                    <thead>
                      <tr>
                        <th>Run ID</th>
                        <th>Session</th>
                        <th>First Call</th>
                        <th>Cost</th>
                      </tr>
                    </thead>
                    <tbody>
                      {day.runs.map((r) => (
                        <tr key={r.run_id} className="cursor-pointer hover:bg-panel-alt" onClick={() => openRunDetail(r.run_id)}>
                          <td>{r.run_id}</td>
                          <td>{r.session_prefix || "—"}</td>
                          <td>{fmtTime(r.first_timestamp)}</td>
                          <td>{fmtMoney(r.total_cost_usd)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>,
                ]
              : []}
          </EvidenceSection>

          <EvidenceSection title="Trades this day" emptyText="No trades recorded for this day.">
            {day.trades.length
              ? [
                  <table key="trades">
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Symbol</th>
                        <th>Action</th>
                        <th>Qty</th>
                        <th>Price</th>
                        <th>Fill</th>
                      </tr>
                    </thead>
                    <tbody>
                      {day.trades.map((t) => (
                        <tr key={t.id}>
                          <td>{fmtTime(t.timestamp)}</td>
                          <td>{t.symbol}</td>
                          <td>
                            <Pill text={t.action} />
                          </td>
                          <td>{fmtNum(t.qty)}</td>
                          <td>{fmtMoney(t.price)}</td>
                          <td>
                            <Pill text={t.fill_status || "unfilled"} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>,
                ]
              : []}
          </EvidenceSection>

          <EvidenceSection title="Candidates considered" emptyText="No candidates recorded for this day.">
            {day.candidates.length
              ? [
                  <div key="chips">
                    {day.candidates.map((sym) => (
                      <button
                        key={sym}
                        type="button"
                        className="candidate-chip"
                        onClick={() => onOpenCandidate(day.runs, sym)}
                      >
                        {sym}
                      </button>
                    ))}
                  </div>,
                ]
              : []}
          </EvidenceSection>
        </div>
      )}
    </Panel>
  );
}
