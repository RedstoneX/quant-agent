import { KeyboardEvent, useEffect, useMemo, useState } from "react";
import {
  api,
  CandidateFunnelItem,
  DecisionState,
  JournalDayResponse,
  RunFunnelResponse,
  RunSummary,
  TradeItem,
} from "../api/client";
import { fmtMoney, fmtNum, fmtPct, fmtTime, pnlClass } from "../lib/format";
import { parseJsonArray, summarizeBlobItem } from "../lib/blobJson";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { Card, CardText, EvidenceSection, KV } from "./ui/Evidence";
import { useModalActions } from "../context/ModalContext";

// Mirrors DecisionFunnelPanel's STATE_LABELS/STATE_COLORS mapping so the
// language is consistent across the cockpit, duplicated locally (rather
// than imported) to keep this file's per-day-multi-run layout independent
// of the single-run panel's presentation.
const STATE_LABELS: Record<DecisionState, string> = {
  executed: "EXECUTED",
  proposed_not_executed: "PROPOSED — NOT EXECUTED",
  hard_risk_block: "DETERMINISTIC GATE BLOCKED",
  no_proposal: "NO TRADE — PM STAYED NEUTRAL",
  no_candidates: "NO CANDIDATES CONSIDERED",
};

const STATE_COLORS: Record<DecisionState, string> = {
  executed: "bg-pos/15 text-pos border-pos/40",
  proposed_not_executed: "bg-warn/15 text-warn border-warn/40",
  hard_risk_block: "bg-neg/15 text-neg border-neg/40",
  no_proposal: "bg-dim/15 text-dim border-border",
  no_candidates: "bg-dim/15 text-dim border-border",
};

const NO_TRADE_REASON: Partial<Record<DecisionState, string>> = {
  hard_risk_block: "Deterministic risk gate blocked this run before any trade could be considered.",
  proposed_not_executed: "The Portfolio Manager proposed a trade this run, but it was not executed.",
  no_proposal: "No trade — the Portfolio Manager stayed neutral this run.",
};

function BlobList({
  title,
  items,
  fields,
}: {
  title: string;
  items: Record<string, unknown>[];
  fields: string[];
}) {
  return (
    <>
      <div className="text-[0.65rem] text-dim uppercase tracking-wide font-semibold mt-2">{title}</div>
      <ul className="pl-4 text-[0.79rem] list-disc mt-1">
        {items.map((item, i) => (
          <li key={i}>{summarizeBlobItem(item, fields)}</li>
        ))}
      </ul>
    </>
  );
}

function ReflectionCard({ reflection }: { reflection: JournalDayResponse["reflection"] }) {
  if (!reflection) return <StateMessage text="No evening reflection recorded for this day yet." />;

  const missed = parseJsonArray<Record<string, unknown>>(reflection.missed_opportunities_json);
  const sellGrades = parseJsonArray<Record<string, unknown>>(reflection.sell_grades_json);
  const buyGrades = parseJsonArray<Record<string, unknown>>(reflection.buy_grades_json);

  const hasPosture = reflection.tomorrow_bias || reflection.tomorrow_conviction || reflection.risk_rating;
  const hasNarrative =
    reflection.tomorrow_outlook ||
    reflection.tomorrow_key_risks ||
    reflection.lessons ||
    reflection.suggested_actions ||
    reflection.sell_decisions_assessment;
  const hasAnything = hasPosture || hasNarrative || missed || sellGrades || buyGrades;

  return (
    <Card title="Evening reflection">
      {hasPosture && (
        <>
          {reflection.tomorrow_bias && (
            <div className="kv-row">
              <span className="text-dim">Tomorrow bias</span>
              <Pill text={reflection.tomorrow_bias} />
            </div>
          )}
          <KV label="Tomorrow conviction" value={reflection.tomorrow_conviction} />
          <KV label="Risk rating" value={reflection.risk_rating} />
        </>
      )}
      {reflection.tomorrow_outlook && <CardText text={`Tomorrow outlook: ${reflection.tomorrow_outlook}`} />}
      {reflection.tomorrow_key_risks && <CardText text={`Key risks: ${reflection.tomorrow_key_risks}`} dim />}
      {reflection.lessons && <CardText text={`Lessons: ${reflection.lessons}`} />}
      {reflection.suggested_actions && <CardText text={`Suggested actions: ${reflection.suggested_actions}`} />}
      {reflection.sell_decisions_assessment && (
        <CardText text={`Sell-decision assessment: ${reflection.sell_decisions_assessment}`} />
      )}
      {sellGrades && <BlobList title="Sell grades" items={sellGrades} fields={["symbol", "grade", "reason"]} />}
      {buyGrades && <BlobList title="Buy grades" items={buyGrades} fields={["symbol", "grade", "reason"]} />}
      {missed && (
        <BlobList title="Missed opportunities" items={missed} fields={["symbol", "miss_category", "move_pct"]} />
      )}
      {!hasAnything && <StateMessage text="Evening reflection recorded but no fields populated yet." />}
    </Card>
  );
}

function MorningRegimeCard({
  funnel,
  loading,
  runsExist,
}: {
  funnel: RunFunnelResponse | null | undefined;
  loading: boolean;
  runsExist: boolean;
}) {
  if (!runsExist) return <StateMessage text="No runs recorded for this day — no morning regime read available." />;
  if (loading && funnel === undefined) return <StateMessage text="Loading morning regime read…" />;
  if (!funnel) return <StateMessage text="Could not load the day's first run to read the morning regime." />;
  const macro = funnel.macro_context;
  if (!macro) return <StateMessage text="No macro regime evidence recorded for the day's first run." />;
  return (
    <Card title="Morning regime" broader>
      <div className="kv-row">
        <span className="text-dim">Regime</span>
        <Pill text={macro.regime} />
      </div>
      <KV label="Equity outlook" value={macro.equity_outlook} />
      <KV label="Confidence" value={macro.confidence} />
      {macro.summary && <CardText text={macro.summary} />}
    </Card>
  );
}

function DirGlyph({ direction }: { direction: CandidateFunnelItem["direction"] | undefined }) {
  if (!direction || direction === "unknown") return null;
  const glyph = direction === "bullish" ? "▲" : direction === "bearish" ? "▼" : "•";
  const cls = direction === "bullish" ? "text-pos" : direction === "bearish" ? "text-neg" : "text-dim";
  return <span className={`${cls} font-bold`}>{glyph}</span>;
}

function CandidateChip({
  symbol,
  info,
  onClick,
}: {
  symbol: string;
  info?: CandidateFunnelItem;
  onClick: () => void;
}) {
  return (
    <button type="button" className="candidate-chip" onClick={onClick}>
      <DirGlyph direction={info?.direction} />
      <span>{symbol}</span>
      {info?.is_bearish_hedge && <span className="text-hedge text-[0.62rem] font-bold uppercase">hedge</span>}
      {info?.executed && (
        <span className="text-pos text-[0.68rem]" title="Executed this run">
          &#10003;
        </span>
      )}
    </button>
  );
}

function RunNarrativeCard({
  run,
  funnel,
  funnelLoading,
  dayRuns,
  dayTrades,
  onOpenCandidate,
  onOpenRun,
}: {
  run: RunSummary;
  funnel: RunFunnelResponse | null | undefined;
  funnelLoading: boolean;
  dayRuns: RunSummary[];
  dayTrades: TradeItem[];
  onOpenCandidate: (runs: RunSummary[], symbol: string) => void;
  onOpenRun: (runId: string) => void;
}) {
  const runTrades = dayTrades.filter((t) => t.run_id === run.run_id);

  return (
    <div className="card">
      <div className="flex items-center gap-2 flex-wrap mb-2">
        {funnel && (
          <span className={`pill border text-[0.72rem] px-2.5 py-0.5 ${STATE_COLORS[funnel.decision_state]}`}>
            {STATE_LABELS[funnel.decision_state]}
          </span>
        )}
        <button
          type="button"
          className="text-accent underline text-[0.79rem]"
          onClick={() => onOpenRun(run.run_id)}
        >
          run {run.run_id}
        </button>
        <span className="text-dim text-[0.78rem]">
          {run.session_prefix ? `${run.session_prefix} · ` : ""}
          {fmtTime(run.first_timestamp)}
          {run.total_cost_usd !== null ? ` · ${fmtMoney(run.total_cost_usd)}` : ""}
        </span>
      </div>

      {!funnel && funnelLoading && <StateMessage text="Loading decision funnel for this run…" />}
      {!funnel && !funnelLoading && <StateMessage text="Could not load the decision funnel for this run." />}

      {funnel && (
        <>
          <div className="text-[0.78rem] text-dim mb-2">
            {fmtNum(funnel.candidates_considered, 0)} considered &rarr; {fmtNum(funnel.reached_pm_count, 0)} reached PM
            &rarr; {fmtNum(funnel.proposed_order_count, 0)} proposed &rarr; {fmtNum(funnel.executed_count, 0)} executed
          </div>

          {funnel.decision_state === "hard_risk_block" && (
            <div className="mb-2 px-2.5 py-1.5 rounded-lg border border-neg/40 bg-neg/10 text-neg text-[0.79rem] font-semibold">
              Deterministic hard-risk gate blocked every candidate this run before the AI Risk Manager was called.
            </div>
          )}

          {funnel.candidates.length === 0 ? (
            <StateMessage text="No candidates considered this run." />
          ) : (
            <div className="flex flex-col gap-1.5">
              {funnel.candidates.map((c) => (
                <div key={c.symbol} className="flex items-start gap-2 text-[0.79rem] flex-wrap">
                  <CandidateChip symbol={c.symbol} info={c} onClick={() => onOpenCandidate(dayRuns, c.symbol)} />
                  <span className="text-dim">
                    {c.proposed_action ? `PM: ${c.proposed_action}` : "PM: no proposal"}
                    {c.risk_modified ? " · risk-modified" : ""}
                    {c.executed
                      ? ` · executed${c.trade_action ? ` (${c.trade_action})` : ""}`
                      : c.reached_proposed_order
                      ? " · not executed"
                      : ""}
                  </span>
                </div>
              ))}
            </div>
          )}

          {funnel.pm_reasoning?.portfolio_view && (
            <div className="mt-2">
              <div className="text-[0.65rem] text-dim uppercase tracking-wide font-semibold mb-0.5">
                Portfolio Manager
              </div>
              <CardText text={funnel.pm_reasoning.portfolio_view} />
            </div>
          )}

          {funnel.risk_verdict?.verdict && (
            <div className="mt-2">
              <div className="text-[0.65rem] text-dim uppercase tracking-wide font-semibold mb-0.5">
                AI Risk Manager
              </div>
              <div className="kv-row">
                <span className="text-dim">Verdict</span>
                <Pill text={funnel.risk_verdict.verdict.approved ? "approved" : "rejected"} />
              </div>
              <CardText text={funnel.risk_verdict.verdict.reasoning} />
            </div>
          )}

          <div className="mt-2">
            <div className="text-[0.65rem] text-dim uppercase tracking-wide font-semibold mb-0.5">
              Trades this run
            </div>
            {runTrades.length > 0 ? (
              <ul className="pl-4 text-[0.79rem] list-disc">
                {runTrades.map((t) => (
                  <li key={t.id}>
                    {t.action} {fmtNum(t.qty)} {t.symbol} @ {fmtMoney(t.price)} ({t.fill_status || "unfilled"})
                  </li>
                ))}
              </ul>
            ) : (
              <StateMessage text={NO_TRADE_REASON[funnel.decision_state] || "No trades recorded for this run."} />
            )}
          </div>
        </>
      )}
    </div>
  );
}

export function JournalPanel({ onOpenCandidate }: { onOpenCandidate: (runs: RunSummary[], symbol: string) => void }) {
  const [dates, setDates] = useState<string[]>([]);
  const [date, setDate] = useState<string>("");
  const [day, setDay] = useState<JournalDayResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [funnels, setFunnels] = useState<Record<string, RunFunnelResponse | null>>({});
  const [funnelsLoading, setFunnelsLoading] = useState(false);
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

  // Once the day's runs are known, pull each run's decision funnel in
  // parallel — this is what powers the morning-regime read, per-run
  // specialist/PM/risk narrative, and enriched candidate chips below.
  useEffect(() => {
    if (!day || !day.runs.length) {
      setFunnels({});
      return;
    }
    let cancelled = false;
    setFunnelsLoading(true);
    Promise.all(
      day.runs.map((r) =>
        api
          .runFunnel(r.run_id)
          .then((f): [string, RunFunnelResponse | null] => [r.run_id, f])
          .catch((): [string, RunFunnelResponse | null] => [r.run_id, null])
      )
    ).then((pairs) => {
      if (cancelled) return;
      setFunnels(Object.fromEntries(pairs));
      setFunnelsLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [day]);

  const sortedRuns = useMemo(
    () =>
      day
        ? [...day.runs].sort((a, b) => (a.first_timestamp || "").localeCompare(b.first_timestamp || ""))
        : [],
    [day]
  );

  const candidateInfo = useMemo(() => {
    const map: Record<string, CandidateFunnelItem> = {};
    for (const r of sortedRuns) {
      const f = funnels[r.run_id];
      if (!f) continue;
      for (const c of f.candidates) {
        if (!map[c.symbol] || c.executed) map[c.symbol] = c;
      }
    }
    return map;
  }, [sortedRuns, funnels]);

  const anyHardBlock = sortedRuns.some((r) => funnels[r.run_id]?.hard_risk_block);

  // Prev/next navigation over `dates` — computed via a lexically sorted
  // copy so the buttons work regardless of the order the API returns
  // dates in (rather than assuming index 0 is newest or oldest).
  const sortedDates = useMemo(() => [...dates].sort(), [dates]);
  const dateIdx = date ? sortedDates.indexOf(date) : -1;
  const olderDate = dateIdx > 0 ? sortedDates[dateIdx - 1] : null;
  const newerDate = dateIdx >= 0 && dateIdx < sortedDates.length - 1 ? sortedDates[dateIdx + 1] : null;

  function handleNavKey(e: KeyboardEvent<HTMLButtonElement>) {
    if (e.key === "ArrowLeft" && olderDate) {
      e.preventDefault();
      setDate(olderDate);
    } else if (e.key === "ArrowRight" && newerDate) {
      e.preventDefault();
      setDate(newerDate);
    }
  }

  const status = error ? "error" : loading ? "loading" : "ok";
  const firstRun = sortedRuns[0];
  const firstFunnel = firstRun ? funnels[firstRun.run_id] : undefined;

  return (
    <Panel
      title="Journal"
      subtitle="Prior trading day, read as a narrative: morning regime, specialist views, PM/risk decisions, trades, and evening reflection."
      status={status}
      full
      accent
      actions={
        dates.length > 0 && (
          <div className="flex items-center gap-1">
            <button
              type="button"
              disabled={!olderDate}
              onClick={() => olderDate && setDate(olderDate)}
              onKeyDown={handleNavKey}
              aria-label="Previous day"
              title="Previous day (Left arrow when focused)"
              className="bg-panel-alt border border-border rounded text-[0.78rem] px-1.5 py-0.5 disabled:opacity-30 disabled:cursor-not-allowed hover:border-accent hover:text-accent disabled:hover:border-border disabled:hover:text-ink"
            >
              &#8249;
            </button>
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
            <button
              type="button"
              disabled={!newerDate}
              onClick={() => newerDate && setDate(newerDate)}
              onKeyDown={handleNavKey}
              aria-label="Next day"
              title="Next day (Right arrow when focused)"
              className="bg-panel-alt border border-border rounded text-[0.78rem] px-1.5 py-0.5 disabled:opacity-30 disabled:cursor-not-allowed hover:border-accent hover:text-accent disabled:hover:border-border disabled:hover:text-ink"
            >
              &#8250;
            </button>
          </div>
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

          {anyHardBlock && (
            <div className="mb-3 px-3 py-2 rounded-lg border border-neg/40 bg-neg/10 text-neg text-[0.82rem] font-semibold">
              The deterministic risk gate blocked at least one run today — see the flagged run below.
            </div>
          )}

          <EvidenceSection title="Morning thesis / regime">
            {[
              <MorningRegimeCard
                key="morning"
                funnel={firstFunnel}
                loading={funnelsLoading}
                runsExist={sortedRuns.length > 0}
              />,
            ]}
          </EvidenceSection>

          <EvidenceSection title="Runs this day — decisions" emptyText="No runs recorded for this day.">
            {sortedRuns.map((r) => (
              <RunNarrativeCard
                key={r.run_id}
                run={r}
                funnel={funnels[r.run_id]}
                funnelLoading={funnelsLoading}
                dayRuns={day.runs}
                dayTrades={day.trades}
                onOpenCandidate={onOpenCandidate}
                onOpenRun={openRunDetail}
              />
            ))}
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
                      <CandidateChip
                        key={sym}
                        symbol={sym}
                        info={candidateInfo[sym]}
                        onClick={() => onOpenCandidate(day.runs, sym)}
                      />
                    ))}
                  </div>,
                ]
              : []}
          </EvidenceSection>

          <EvidenceSection title="Missed opportunities & evening reflection">
            {[<ReflectionCard key="r" reflection={day.reflection} />]}
          </EvidenceSection>
        </div>
      )}
    </Panel>
  );
}
