import { useEffect, useState } from "react";
import { api, CandidateFunnelItem, DecisionState, RunFunnelResponse } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";

// How many recent runs to aggregate over. QAMC runs a handful of scheduled
// sessions per trading day, so this covers roughly the last 1-2 weeks of
// activity — enough for a meaningful sample while keeping the client-side
// fan-out (one /runs/{id}/funnel fetch per run, done in parallel) cheap.
// If QAMC's run cadence ever grows enough that this fan-out gets heavy,
// a server-side aggregation endpoint would be the right fix instead of
// raising this number indefinitely.
const AGGREGATION_WINDOW_RUNS = 25;

type Direction = CandidateFunnelItem["direction"];

const DECISION_STATE_ORDER: DecisionState[] = [
  "executed",
  "proposed_not_executed",
  "hard_risk_block",
  "no_proposal",
  "no_candidates",
];

const DECISION_STATE_LABELS: Record<DecisionState, string> = {
  executed: "EXECUTED",
  proposed_not_executed: "PROPOSED — NOT EXECUTED",
  hard_risk_block: "DETERMINISTIC GATE BLOCKED",
  no_proposal: "NO TRADE — PM STAYED NEUTRAL",
  no_candidates: "NO CANDIDATES CONSIDERED",
};

const DECISION_STATE_BAR_CLASS: Record<DecisionState, string> = {
  executed: "bg-pos",
  proposed_not_executed: "bg-warn",
  hard_risk_block: "bg-neg",
  no_proposal: "bg-dim",
  no_candidates: "bg-dim",
};

interface DirectionCounts {
  bullish: number;
  bearish: number;
  neutralOrUnknown: number;
}

interface RiskBucket {
  approved: number;
  rejected: number;
  total: number;
}

interface Aggregates {
  runsIncluded: number;
  runsFetchFailed: number;

  totalCandidates: number;
  // Instrument's own signal direction (tech_analyst rating on the symbol).
  candidateDirectionCounts: DirectionCounts;
  // Effective market/portfolio exposure direction — instrument direction
  // with inverse-ETF candidates flipped via exposureDirection(). This is
  // the number that actually answers "is QAMC structurally long-only?".
  candidateExposureCounts: DirectionCounts;

  hedgeRunsConsidered: number;
  hedgeCandidatesCount: number;
  hedgeCandidatesProposed: number;
  hedgeCandidatesExecuted: number;

  proposedTotal: number;
  proposedDirectionCounts: DirectionCounts;
  proposedExposureCounts: DirectionCounts;
  proposedActionCounts: Record<string, number>;

  riskReachingRuns: number;
  riskHedgeRuns: RiskBucket;
  riskNonHedgeRuns: RiskBucket;

  decisionStateCounts: Record<DecisionState, number>;
}

function emptyDirectionCounts(): DirectionCounts {
  return { bullish: 0, bearish: 0, neutralOrUnknown: 0 };
}

function bucketDirection(counts: DirectionCounts, direction: Direction) {
  if (direction === "bullish") counts.bullish += 1;
  else if (direction === "bearish") counts.bearish += 1;
  else counts.neutralOrUnknown += 1;
}

// `CandidateFunnelItem.direction` is the *instrument's own* signal
// direction (tech_analyst's rating on the symbol itself) — it is NOT the
// resulting market/portfolio exposure direction. For an ordinary long
// instrument the two coincide: a bullish signal expresses bullish
// exposure. For an approved inverse ETF (`is_bearish_hedge` — SH/SDS/
// PSQ/SQQQ, see src/api/deps.py::INVERSE_ETF_SYMBOLS on the backend),
// being bullish ON THE INSTRUMENT (e.g. BUY SQQQ) expresses BEARISH
// exposure to the underlying index, and vice versa — so exposure
// direction is the *inverse* of instrument direction there. This is a
// panel meant to diagnose structural long bias, so silently counting an
// inverse-ETF BUY as "bullish" would hide the exact bearish positioning
// it exists to surface. Always derived from the API's own
// `is_bearish_hedge` flag, never from the symbol name.
function exposureDirection(direction: Direction, isBearishHedge: boolean): Direction {
  if (!isBearishHedge) return direction;
  if (direction === "bullish") return "bearish";
  if (direction === "bearish") return "bullish";
  return direction; // neutral/unknown carries no exposure to flip
}

// Pure aggregation over already-fetched funnel data — kept separate from
// the fetching effect below so the counting logic is easy to trace/verify
// by hand against a small hypothetical dataset.
function computeAggregates(funnels: RunFunnelResponse[], fetchFailed: number): Aggregates {
  const candidateDirectionCounts = emptyDirectionCounts();
  const candidateExposureCounts = emptyDirectionCounts();
  const proposedDirectionCounts = emptyDirectionCounts();
  const proposedExposureCounts = emptyDirectionCounts();
  const proposedActionCounts: Record<string, number> = {};
  const decisionStateCounts: Record<DecisionState, number> = {
    executed: 0,
    proposed_not_executed: 0,
    hard_risk_block: 0,
    no_proposal: 0,
    no_candidates: 0,
  };

  let totalCandidates = 0;
  let hedgeRunsConsidered = 0;
  let hedgeCandidatesCount = 0;
  let hedgeCandidatesProposed = 0;
  let hedgeCandidatesExecuted = 0;
  let proposedTotal = 0;

  let riskReachingRuns = 0;
  const riskHedgeRuns: RiskBucket = { approved: 0, rejected: 0, total: 0 };
  const riskNonHedgeRuns: RiskBucket = { approved: 0, rejected: 0, total: 0 };

  for (const f of funnels) {
    decisionStateCounts[f.decision_state] = (decisionStateCounts[f.decision_state] ?? 0) + 1;
    if (f.bearish_hedge_considered) hedgeRunsConsidered += 1;

    for (const c of f.candidates) {
      totalCandidates += 1;
      bucketDirection(candidateDirectionCounts, c.direction);
      bucketDirection(candidateExposureCounts, exposureDirection(c.direction, c.is_bearish_hedge));

      if (c.is_bearish_hedge) {
        hedgeCandidatesCount += 1;
        if (c.reached_proposed_order) hedgeCandidatesProposed += 1;
        if (c.executed) hedgeCandidatesExecuted += 1;
      }

      if (c.reached_proposed_order) {
        proposedTotal += 1;
        bucketDirection(proposedDirectionCounts, c.direction);
        bucketDirection(proposedExposureCounts, exposureDirection(c.direction, c.is_bearish_hedge));
        const action = c.proposed_action || "none";
        proposedActionCounts[action] = (proposedActionCounts[action] ?? 0) + 1;
      }
    }

    // risk_verdict is run-wide, not per-candidate — see the caveat rendered
    // alongside this data below. We can only honestly bucket by whether the
    // run involved a bearish-hedge candidate, not by individual direction.
    const verdict = f.risk_verdict?.verdict;
    if (verdict) {
      riskReachingRuns += 1;
      const bucket = f.bearish_hedge_considered ? riskHedgeRuns : riskNonHedgeRuns;
      bucket.total += 1;
      if (verdict.approved) bucket.approved += 1;
      else bucket.rejected += 1;
    }
  }

  return {
    runsIncluded: funnels.length,
    runsFetchFailed: fetchFailed,
    totalCandidates,
    candidateDirectionCounts,
    candidateExposureCounts,
    hedgeRunsConsidered,
    hedgeCandidatesCount,
    hedgeCandidatesProposed,
    hedgeCandidatesExecuted,
    proposedTotal,
    proposedDirectionCounts,
    proposedExposureCounts,
    proposedActionCounts,
    riskReachingRuns,
    riskHedgeRuns,
    riskNonHedgeRuns,
    decisionStateCounts,
  };
}

function DirectionRatioBar({ counts, total }: { counts: DirectionCounts; total: number }) {
  if (total === 0) return null;
  const segments: { label: string; count: number; barClass: string; textClass: string }[] = [
    { label: "bullish", count: counts.bullish, barClass: "bg-pos", textClass: "text-pos" },
    { label: "bearish", count: counts.bearish, barClass: "bg-neg", textClass: "text-neg" },
    { label: "neutral/unknown", count: counts.neutralOrUnknown, barClass: "bg-dim", textClass: "text-dim" },
  ];
  return (
    <div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-panel-alt border border-border">
        {segments.map(
          (s) =>
            s.count > 0 && (
              <div
                key={s.label}
                className={s.barClass}
                style={{ width: `${(s.count / total) * 100}%` }}
                title={`${s.label}: ${s.count} (${((s.count / total) * 100).toFixed(0)}%)`}
              />
            )
        )}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1.5 text-[0.72rem]">
        {segments.map((s) => (
          <span key={s.label} className={s.textClass}>
            {s.label} {s.count}
            <span className="text-dim"> ({((s.count / total) * 100).toFixed(0)}%)</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function StatRow({ label, count, total, barClass }: { label: string; count: number; total: number; barClass: string }) {
  const pct = total > 0 ? (count / total) * 100 : 0;
  return (
    <div className="mb-2 last:mb-0">
      <div className="flex justify-between text-[0.75rem] mb-0.5">
        <span>{label}</span>
        <span className="tabular-nums text-dim">
          {count} of {total} ({pct.toFixed(0)}%)
        </span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-panel-alt overflow-hidden">
        <div className={`h-full ${barClass}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function DirectionalBiasPanel() {
  const [agg, setAgg] = useState<Aggregates | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .runs(AGGREGATION_WINDOW_RUNS)
      .then((r) =>
        Promise.all(
          r.runs.map((run) =>
            api
              .runFunnel(run.run_id)
              .then((f) => ({ ok: true as const, f }))
              .catch(() => ({ ok: false as const }))
          )
        )
      )
      .then((results) => {
        if (cancelled) return;
        const funnels = results.filter((x): x is { ok: true; f: RunFunnelResponse } => x.ok).map((x) => x.f);
        setAgg(computeAggregates(funnels, results.length - funnels.length));
        setError(null);
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
  const dominantState =
    agg && agg.runsIncluded > 0
      ? DECISION_STATE_ORDER.reduce((best, s) =>
          agg.decisionStateCounts[s] > agg.decisionStateCounts[best] ? s : best
        , DECISION_STATE_ORDER[0])
      : null;

  return (
    <Panel
      title="Directional bias across recent runs"
      subtitle="Read-only lens on past runs — not a trading signal or recommendation."
      status={status}
      full
      accent
    >
      {error && <StateMessage text={`Could not load directional-bias data: ${error}`} error />}
      {!error && loading && !agg && <StateMessage text="Loading…" />}
      {!error && agg && agg.runsIncluded === 0 && (
        <StateMessage text="No runs recorded yet in this window — not enough data for directional-bias stats." />
      )}

      {!error && agg && agg.runsIncluded > 0 && (
        <div className="flex flex-col gap-5">
          <div className="text-dim text-[0.72rem]">
            Window: last {agg.runsIncluded} of up to {AGGREGATION_WINDOW_RUNS} runs requested
            {agg.runsFetchFailed > 0 ? ` (${agg.runsFetchFailed} run${agg.runsFetchFailed === 1 ? "" : "s"} failed to load and were excluded)` : ""}.
          </div>

          {/* Instrument signal direction vs effective market exposure —
              deliberately shown as two separate bars, never collapsed into
              one number. A bullish signal on an inverse ETF (SQQQ, etc.)
              expresses BEARISH market exposure; conflating the two would
              hide exactly the structural-long-bias signal this panel
              exists to surface. */}
          <div>
            <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-2 pb-1 border-b border-border">
              Candidates considered — direction
            </div>
            {agg.totalCandidates === 0 ? (
              <StateMessage text="No candidates were considered in this window." />
            ) : (
              <div className="flex flex-col gap-3">
                <div>
                  <div className="text-[0.68rem] text-dim uppercase tracking-wide mb-1">Instrument signal direction</div>
                  <DirectionRatioBar counts={agg.candidateDirectionCounts} total={agg.totalCandidates} />
                </div>
                <div>
                  <div className="text-[0.68rem] text-dim uppercase tracking-wide mb-1">
                    Effective market exposure{" "}
                    <span className="normal-case font-normal text-dim">(inverse-ETF candidates flipped)</span>
                  </div>
                  <DirectionRatioBar counts={agg.candidateExposureCounts} total={agg.totalCandidates} />
                </div>
                <div className="text-dim text-[0.72rem]">
                  {agg.totalCandidates} candidate consideration(s) total
                  {agg.hedgeCandidatesCount > 0
                    ? ` — ${agg.hedgeCandidatesCount} on an inverse ETF, where a bullish instrument signal expresses bearish market exposure (and vice versa).`
                    : "."}
                </div>
              </div>
            )}
          </div>

          {/* Inverse-ETF / bearish-hedge consideration */}
          <div>
            <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-2 pb-1 border-b border-border">
              Inverse-ETF (bearish-hedge) consideration
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-center">
              <div className="card">
                <div className="text-[1.05rem] font-extrabold tabular-nums text-hedge">{agg.hedgeRunsConsidered}</div>
                <div className="text-[0.62rem] text-dim uppercase tracking-wide mt-0.5">Runs w/ hedge candidate</div>
              </div>
              <div className="card">
                <div className="text-[1.05rem] font-extrabold tabular-nums text-hedge">{agg.hedgeCandidatesCount}</div>
                <div className="text-[0.62rem] text-dim uppercase tracking-wide mt-0.5">Hedge candidates</div>
              </div>
              <div className="card">
                <div className="text-[1.05rem] font-extrabold tabular-nums text-hedge">
                  {agg.hedgeCandidatesCount > 0 ? `${agg.hedgeCandidatesProposed} of ${agg.hedgeCandidatesCount}` : "—"}
                </div>
                <div className="text-[0.62rem] text-dim uppercase tracking-wide mt-0.5">Reached proposed order</div>
              </div>
              <div className="card">
                <div className="text-[1.05rem] font-extrabold tabular-nums text-hedge">
                  {agg.hedgeCandidatesCount > 0 ? `${agg.hedgeCandidatesExecuted} of ${agg.hedgeCandidatesCount}` : "—"}
                </div>
                <div className="text-[0.62rem] text-dim uppercase tracking-wide mt-0.5">Executed</div>
              </div>
            </div>
            {agg.hedgeCandidatesCount === 0 && (
              <div className="state-message mt-1.5">No bearish-hedge candidates were considered in this window.</div>
            )}
          </div>

          {/* PM directional proposals */}
          <div>
            <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-2 pb-1 border-b border-border">
              PM proposals — candidates that reached a proposed order
            </div>
            {agg.proposedTotal === 0 ? (
              <StateMessage text="No candidates reached a proposed order in this window." />
            ) : (
              <div className="flex flex-col gap-2.5">
                <div>
                  <div className="text-[0.68rem] text-dim uppercase tracking-wide mb-1">Instrument signal direction</div>
                  <DirectionRatioBar counts={agg.proposedDirectionCounts} total={agg.proposedTotal} />
                </div>
                <div>
                  <div className="text-[0.68rem] text-dim uppercase tracking-wide mb-1">
                    Effective market exposure{" "}
                    <span className="normal-case font-normal text-dim">(inverse-ETF candidates flipped)</span>
                  </div>
                  <DirectionRatioBar counts={agg.proposedExposureCounts} total={agg.proposedTotal} />
                </div>
                <div className="flex flex-wrap gap-2 items-center">
                  <span className="text-dim text-[0.72rem]">Proposed actions:</span>
                  {Object.entries(agg.proposedActionCounts).map(([action, count]) => (
                    <span key={action} className="inline-flex items-center gap-1.5">
                      <Pill text={action === "none" ? "no action" : action} />
                      <span className="text-dim text-[0.72rem]">{count}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Risk approvals/rejections by direction (run-level granularity) */}
          <div>
            <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-2 pb-1 border-b border-border">
              AI Risk Manager verdicts, by run
            </div>
            <div className="text-dim text-[0.72rem] mb-2">
              The risk verdict is recorded per run, not per candidate — a run can cover multiple candidates under one
              verdict. Grouped here by whether the run involved a bearish-hedge candidate, not by individual direction.
            </div>
            {agg.riskReachingRuns === 0 ? (
              <StateMessage text="No runs reached the AI Risk Manager in this window." />
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <div className="text-[0.78rem] font-semibold mb-1">
                    Runs with a bearish-hedge candidate <span className="text-hedge">({agg.riskHedgeRuns.total})</span>
                  </div>
                  {agg.riskHedgeRuns.total === 0 ? (
                    <div className="state-message">None reached the Risk Manager in this window.</div>
                  ) : (
                    <StatRow label="Approved" count={agg.riskHedgeRuns.approved} total={agg.riskHedgeRuns.total} barClass="bg-pos" />
                  )}
                </div>
                <div>
                  <div className="text-[0.78rem] font-semibold mb-1">
                    Runs without a bearish-hedge candidate <span className="text-dim">({agg.riskNonHedgeRuns.total})</span>
                  </div>
                  {agg.riskNonHedgeRuns.total === 0 ? (
                    <div className="state-message">None reached the Risk Manager in this window.</div>
                  ) : (
                    <StatRow
                      label="Approved"
                      count={agg.riskNonHedgeRuns.approved}
                      total={agg.riskNonHedgeRuns.total}
                      barClass="bg-pos"
                    />
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Dominant no-trade reason / outcome histogram */}
          <div>
            <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-2 pb-1 border-b border-border">
              Outcome across window
            </div>
            {dominantState && (
              <div className="text-[0.8rem] mb-2.5">
                Most common outcome this window: <strong>{DECISION_STATE_LABELS[dominantState]}</strong> (
                {agg.decisionStateCounts[dominantState]} of {agg.runsIncluded} runs).
              </div>
            )}
            <div>
              {DECISION_STATE_ORDER.map((s) => (
                <StatRow
                  key={s}
                  label={DECISION_STATE_LABELS[s]}
                  count={agg.decisionStateCounts[s]}
                  total={agg.runsIncluded}
                  barClass={DECISION_STATE_BAR_CLASS[s]}
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

// Exported for unit testing (DirectionalBiasPanel.test.ts) — the pure
// aggregation/derivation logic, kept separate from data-fetching and
// rendering so it's directly verifiable without mounting a component.
export { exposureDirection, computeAggregates };
export type { Aggregates, DirectionCounts };
