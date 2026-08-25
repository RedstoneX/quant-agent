import { KeyboardEvent, useEffect, useMemo, useState } from "react";
import {
  api,
  AccountResponse,
  CandidateDetailResponse,
  CandidateFunnelItem,
  DailyPnlPoint,
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
import { LevelBar } from "./ui/Meter";
import { useModalActions } from "../context/ModalContext";
import { Stage, STAGE_META, STAGE_ORDER, candidateStage, isSweepOnlyExecution } from "./funnelShared";
import { buildEntries } from "./agentflow/buildGraph";
import { TradeTable } from "./TradesPanel";

export function ledgerLine(c: CandidateFunnelItem, funnel: RunFunnelResponse): string {
  const pm = c.proposed_action
    ? `PM ${c.proposed_action}`
    : c.reached_pm_target
    ? "PM target, no order — reason not recorded"
    : "PM no proposal";

  let risk = "Risk —";
  let riskExplainsNonExecution = false;
  if (c.reached_proposed_order) {
    const verdict = funnel.risk_verdict?.verdict;
    if (c.risk_modified) {
      risk = "Risk MODIFIED";
    } else if (funnel.hard_risk_block) {
      risk = "Risk BLOCKED (hard gate)";
      riskExplainsNonExecution = true;
    } else if (verdict?.approved === false) {
      risk = "Risk REJECTED";
      riskExplainsNonExecution = true;
    } else if (verdict?.approved === true) {
      risk = "Risk APPROVED";
    }
  }

  let outcome: string;
  if (c.executed) {
    outcome = `EXECUTED${c.trade_action ? ` (${c.trade_action})` : ""}`;
  } else if (c.execution_skip_reason) {
    outcome = `SKIPPED — ${c.execution_skip_reason.replace(/_/g, " ")} · NOT EXECUTED`;
  } else if (c.reached_proposed_order && !riskExplainsNonExecution) {
    outcome = "NOT EXECUTED — execution reason not recorded";
  } else if (c.reached_proposed_order) {
    outcome = "NOT EXECUTED";
  } else {
    outcome = "no order reached";
  }
  return `${pm} · ${risk} · ${outcome}`;
}

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

const SWEEP_ONLY_LABEL = "CASH SWEEP ONLY";
const SWEEP_ONLY_COLOR = "bg-dim/15 text-dim border-border";

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

// A small real-data timeline strip — recent daily P&L from the same
// `account.history` the top strip's equity figure already comes from
// (AccountResponse.history, up to 30 days). No new fetch, no synthesized
// values: bar height is |daily_pnl| scaled to the window's own max, color
// is the real sign. Gives the journal a graphical "how did this stretch
// go" glance before reading any single day's narrative below.
function EquitySparkline({ history }: { history: DailyPnlPoint[] | undefined }) {
  if (!history || history.length === 0) return null;
  const recent = history.slice(-20);
  const max = Math.max(...recent.map((d) => Math.abs(d.daily_pnl ?? 0)), 1);
  return (
    <div className="flex items-end gap-1 h-9 mb-3.5" title="Recent daily P&L">
      {recent.map((d) => {
        const pnl = d.daily_pnl ?? 0;
        const h = Math.max(3, (Math.abs(pnl) / max) * 30);
        const cls = pnl > 0 ? "bg-pos" : pnl < 0 ? "bg-neg" : "bg-dim";
        return (
          <div
            key={d.date}
            className={`w-2 rounded-sm flex-shrink-0 ${cls}`}
            style={{ height: `${h}px` }}
            title={`${d.date}: ${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}`}
          />
        );
      })}
    </div>
  );
}

// Day-level furthest-stage bucket, simplified from funnelShared's
// candidateStage: this list spans potentially several runs with no single
// funnel to read hard_risk_block/risk_verdict from, so it deliberately
// omits candidateStage's run-wide risk-rejection escalation rather than
// guess which run's verdict would apply to a given symbol. Still exact for
// executed/risk_modified/reached_proposed_order/reached_pm_target, all of
// which are precise per-candidate fields.
function daySimpleStage(c: CandidateFunnelItem | undefined): Stage {
  if (!c) return "rejected";
  if (c.executed) return "executed";
  if (c.risk_modified) return "risk_action";
  if (c.reached_proposed_order) return "proposed";
  if (c.reached_pm_target) return "reached_pm";
  return "rejected";
}

function DayCandidateList({
  symbols,
  info,
  dayRuns,
  onOpenCandidate,
}: {
  symbols: string[];
  info: Record<string, CandidateFunnelItem>;
  dayRuns: RunSummary[];
  onOpenCandidate: (runs: RunSummary[], symbol: string) => void;
}) {
  const buckets: Record<Stage, string[]> = { executed: [], risk_action: [], proposed: [], reached_pm: [], rejected: [] };
  for (const sym of symbols) buckets[daySimpleStage(info[sym])].push(sym);
  const notable = STAGE_ORDER.filter((s) => s !== "rejected").flatMap((s) => buckets[s]);
  const screened = buckets.rejected;

  return (
    <div>
      {notable.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {notable.map((sym) => (
            <CandidateChip key={sym} symbol={sym} info={info[sym]} onClick={() => onOpenCandidate(dayRuns, sym)} />
          ))}
        </div>
      )}
      {screened.length > 0 && (
        <details className={notable.length > 0 ? "mt-2" : undefined}>
          <summary className="text-[0.72rem] text-dim cursor-pointer select-none">
            {screened.length} more screened across the day, no PM target reached &mdash; expand
          </summary>
          <div className="flex flex-wrap gap-1.5 mt-1.5">
            {screened.map((sym) => (
              <CandidateChip key={sym} symbol={sym} info={info[sym]} onClick={() => onOpenCandidate(dayRuns, sym)} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Agent Analysis & Disagreements — docs/OUTCOME.md's Journal Day
 * sections 3-4. Real per-specialist views and the backend's own
 * consensus.agreement computation for each candidate that reached the
 * Portfolio Manager today (the "screened, no PM target" majority has no
 * PM-relevant disagreement to report and is left in the collapsed
 * Watchlist bucket above). Never a fabricated summary: same buildEntries
 * derivation the cockpit's per-candidate agent graph uses, one
 * CandidateDetailResponse fetch per notable candidate — typically a
 * handful a day, the same bounded-fan-out pattern this file already uses
 * for per-run funnels.
 * ------------------------------------------------------------------ */

interface NotableCandidate {
  runId: string;
  symbol: string;
  stage: Stage;
}

function collectNotableCandidates(runs: RunSummary[], funnels: Record<string, RunFunnelResponse | null>): NotableCandidate[] {
  const map: Record<string, NotableCandidate> = {};
  for (const r of runs) {
    const f = funnels[r.run_id];
    if (!f) continue;
    for (const c of f.candidates) {
      const stage = candidateStage(c, f);
      if (stage === "rejected") continue;
      if (!map[c.symbol] || c.executed) map[c.symbol] = { runId: r.run_id, symbol: c.symbol, stage };
    }
  }
  return Object.values(map);
}

const AGREEMENT_LABEL: Record<CandidateDetailResponse["consensus"]["agreement"], string> = {
  aligned: "Aligned",
  mixed: "Diverges",
  no_directional_signal: "No directional signal",
  insufficient_data: "Insufficient data",
};

const AGREEMENT_CLASS: Record<CandidateDetailResponse["consensus"]["agreement"], string> = {
  aligned: "bg-pos/15 text-pos border-pos/40",
  mixed: "bg-warn/15 text-warn border-warn/40",
  no_directional_signal: "bg-dim/15 text-dim border-border",
  insufficient_data: "bg-dim/15 text-dim border-border",
};

function DayAgentAnalysis({
  candidates,
  dayRuns,
  onOpenCandidate,
}: {
  candidates: NotableCandidate[];
  dayRuns: RunSummary[];
  onOpenCandidate: (runs: RunSummary[], symbol: string) => void;
}) {
  const [details, setDetails] = useState<Record<string, CandidateDetailResponse | null>>({});
  const [loading, setLoading] = useState(true);
  const key = candidates.map((c) => `${c.runId}:${c.symbol}`).join(",");

  useEffect(() => {
    if (!candidates.length) {
      setDetails({});
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    Promise.all(
      candidates.map((c) =>
        api
          .candidateDetail(c.runId, c.symbol)
          .then((d): [string, CandidateDetailResponse | null] => [c.symbol, d])
          .catch((): [string, CandidateDetailResponse | null] => [c.symbol, null])
      )
    ).then((pairs) => {
      if (cancelled) return;
      setDetails(Object.fromEntries(pairs));
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  if (!candidates.length) {
    return <StateMessage text="No candidates reached the Portfolio Manager today — nothing for specialists to have agreed or disagreed on." />;
  }
  if (loading) return <StateMessage text="Loading specialist analysis…" />;

  const disagreements = candidates.filter((c) => details[c.symbol]?.consensus.agreement === "mixed");

  return (
    <div className="flex flex-col gap-2">
      {candidates.map((c) => {
        const d = details[c.symbol];
        if (!d) return null;
        const entries = buildEntries(d);
        return (
          <div key={c.symbol} className="card">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <button
                type="button"
                className="font-bold text-accent underline text-[0.82rem]"
                onClick={() => onOpenCandidate(dayRuns, c.symbol)}
              >
                {c.symbol}
              </button>
              <span className={`text-[0.68rem] font-semibold uppercase tracking-wide ${STAGE_META[c.stage].textClass}`}>
                {STAGE_META[c.stage].label}
              </span>
              <span className={`ml-auto pill border text-[0.68rem] px-2 py-0.5 ${AGREEMENT_CLASS[d.consensus.agreement]}`}>
                {AGREEMENT_LABEL[d.consensus.agreement]}
              </span>
            </div>
            {entries.length === 0 ? (
              <StateMessage text="No specialist evidence recorded for this candidate." />
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-1.5">
                {entries.map((e) => (
                  <div key={e.key} className="bg-panel-alt rounded-md px-2 py-1.5">
                    <div className="flex items-center justify-between gap-1.5">
                      <span className="text-[0.68rem] font-semibold truncate">{e.role}</span>
                      <span
                        className={`text-[0.78rem] font-bold ${
                          e.direction === "bullish" ? "text-pos" : e.direction === "bearish" ? "text-neg" : "text-dim"
                        }`}
                      >
                        {e.direction === "bullish" ? "▲" : e.direction === "bearish" ? "▼" : "•"}
                      </span>
                    </div>
                    {e.conviction && <LevelBar level={e.conviction} tone="accent" />}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
      {disagreements.length > 0 && (
        <div className="px-3 py-2 rounded-lg border border-warn/40 bg-warn/10 text-warn text-[0.8rem] font-semibold">
          Specialists genuinely disagreed on: {disagreements.map((c) => c.symbol).join(", ")}.
        </div>
      )}
    </div>
  );
}

// Same furthest-stage bucketing CandidateRail uses (funnelShared.ts), so a
// run that screened 80 symbols reads as "the dozen that mattered" plus one
// collapsed count instead of an 80-line flat dump repeating "PM: no
// proposal" — the Journal's own instance of the "chronological log dump"
// docs/OUTCOME.md's Journal Day section warns against, reproduced against
// real production data (a real trading day here runs long past 60 screened
// symbols with zero PM target) while building this.
function RunCandidateList({
  funnel,
  onOpenCandidate,
}: {
  funnel: RunFunnelResponse;
  onOpenCandidate: (symbol: string) => void;
}) {
  const buckets: Record<Stage, CandidateFunnelItem[]> = {
    executed: [],
    risk_action: [],
    proposed: [],
    reached_pm: [],
    rejected: [],
  };
  for (const c of funnel.candidates) buckets[candidateStage(c, funnel)].push(c);
  const notable = STAGE_ORDER.filter((s) => s !== "rejected").flatMap((s) => buckets[s]);
  const screened = buckets.rejected;

  return (
    <div className="flex flex-col gap-1.5">
      {notable.length === 0 && screened.length > 0 && (
        <StateMessage
          text={`${screened.length} candidate${screened.length === 1 ? "" : "s"} screened this run — none reached a Portfolio Manager target.`}
        />
      )}
      {notable.map((c) => {
        const stage = candidateStage(c, funnel);
        return (
          <div key={c.symbol} className="flex items-start gap-2 text-[0.79rem] flex-wrap">
            <CandidateChip symbol={c.symbol} info={c} onClick={() => onOpenCandidate(c.symbol)} />
            <span className={STAGE_META[stage].textClass}>{STAGE_META[stage].label}</span>
            <span className="text-dim">{ledgerLine(c, funnel)}</span>
          </div>
        );
      })}
      {screened.length > 0 && (
        <details className="mt-1">
          <summary className="text-[0.72rem] text-dim cursor-pointer select-none">
            {screened.length} more screened, no PM target reached &mdash; expand
          </summary>
          <div className="flex flex-wrap gap-1.5 mt-1.5">
            {screened.map((c) => (
              <CandidateChip key={c.symbol} symbol={c.symbol} info={c} onClick={() => onOpenCandidate(c.symbol)} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export function RunNarrativeCard({
  run,
  funnel,
  funnelLoading,
  dayTrades,
  onOpenRun,
}: {
  run: RunSummary;
  funnel: RunFunnelResponse | null | undefined;
  funnelLoading: boolean;
  dayTrades: TradeItem[];
  onOpenRun: (runId: string) => void;
}) {
  const { openCandidateDetail } = useModalActions();
  const runTrades = dayTrades.filter((t) => t.run_id === run.run_id);
  const sweepOnly = funnel?.decision_state === "executed" && isSweepOnlyExecution(runTrades);

  return (
    <div className="card">
      <div className="flex items-center gap-2 flex-wrap mb-2">
        {funnel && (
          <span className={`pill border text-[0.72rem] px-2.5 py-0.5 ${sweepOnly ? SWEEP_ONLY_COLOR : STATE_COLORS[funnel.decision_state]}`}>
            {sweepOnly ? SWEEP_ONLY_LABEL : STATE_LABELS[funnel.decision_state]}
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
            <RunCandidateList funnel={funnel} onOpenCandidate={(symbol) => openCandidateDetail(run.run_id, symbol)} />
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

export function JournalPanel({
  account,
  onOpenCandidate,
}: {
  account: AccountResponse | null;
  onOpenCandidate: (runs: RunSummary[], symbol: string) => void;
}) {
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

  const notableCandidates = useMemo(() => collectNotableCandidates(sortedRuns, funnels), [sortedRuns, funnels]);

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
          <EquitySparkline history={account?.history} />
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

          <EvidenceSection title="Market thesis / morning regime">
            {[
              <MorningRegimeCard
                key="morning"
                funnel={firstFunnel}
                loading={funnelsLoading}
                runsExist={sortedRuns.length > 0}
              />,
            ]}
          </EvidenceSection>

          {/* Watchlist/candidates ahead of the per-run decision detail below
              — same day-narrative order as docs/OUTCOME.md's Journal Day
              section (thesis, then watchlist, then decisions), rather than
              leaving the day's full candidate set to appear only after
              every run has already been read in detail. */}
          <EvidenceSection title="Watchlist / candidates considered" emptyText="No candidates recorded for this day.">
            {day.candidates.length ? [<DayCandidateList key="chips" symbols={day.candidates} info={candidateInfo} dayRuns={day.runs} onOpenCandidate={onOpenCandidate} />] : []}
          </EvidenceSection>

          <EvidenceSection title="Agent analysis &amp; disagreements">
            {[
              <DayAgentAnalysis
                key="agent-analysis"
                candidates={notableCandidates}
                dayRuns={day.runs}
                onOpenCandidate={onOpenCandidate}
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
                dayTrades={day.trades}
                onOpenRun={openRunDetail}
              />
            ))}
          </EvidenceSection>

          <EvidenceSection title="Trades this day" emptyText="No trades recorded for this day.">
            {day.trades.length ? [<TradeTable key="trades" trades={day.trades} />] : []}
          </EvidenceSection>

          <EvidenceSection title="Daily result — missed opportunities, lessons &amp; tomorrow">
            {[<ReflectionCard key="r" reflection={day.reflection} />]}
          </EvidenceSection>
        </div>
      )}
    </Panel>
  );
}
