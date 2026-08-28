import { useEffect, useState } from "react";
import { BadgeDelta, DonutChart, type Color } from "@tremor/react";
import { api, CandidateFunnelItem, RunFunnelResponse } from "../api/client";
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

interface DirectionCounts {
  bullish: number;
  bearish: number;
  neutralOrUnknown: number;
}

interface Aggregates {
  runsIncluded: number;
  runsFetchFailed: number;

  totalCandidates: number;
  // Instrument's own signal direction (tech_analyst rating on the symbol).
  candidateDirectionCounts: DirectionCounts;
  // Effective market/portfolio exposure direction — instrument direction
  // with inverse-ETF candidates flipped via exposureDirection(). This is
  // the number that actually answers "is QAMC structurally long-only?",
  // and the one the panel leads with below.
  candidateExposureCounts: DirectionCounts;

  hedgeRunsConsidered: number;
  hedgeCandidatesCount: number;

  proposedTotal: number;
  proposedDirectionCounts: DirectionCounts;
  proposedExposureCounts: DirectionCounts;
  proposedActionCounts: Record<string, number>;
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
//
// Cockpit pass 3, item 3: this used to also tally AI Risk Manager
// approve/reject verdicts and a full decision-state (executed/no-trade/
// blocked) histogram. Both are dropped here, not just re-skinned — see
// the panel's render function below for why (in short: they're a
// different concern than direction, thin samples on real data, and the
// decision-state breakdown duplicates the Runs tab one click away in the
// same workspace group).
function computeAggregates(funnels: RunFunnelResponse[], fetchFailed: number): Aggregates {
  const candidateDirectionCounts = emptyDirectionCounts();
  const candidateExposureCounts = emptyDirectionCounts();
  const proposedDirectionCounts = emptyDirectionCounts();
  const proposedExposureCounts = emptyDirectionCounts();
  const proposedActionCounts: Record<string, number> = {};

  let totalCandidates = 0;
  let hedgeRunsConsidered = 0;
  let hedgeCandidatesCount = 0;
  let proposedTotal = 0;

  for (const f of funnels) {
    if (f.bearish_hedge_considered) hedgeRunsConsidered += 1;

    for (const c of f.candidates) {
      totalCandidates += 1;
      bucketDirection(candidateDirectionCounts, c.direction);
      bucketDirection(candidateExposureCounts, exposureDirection(c.direction, c.is_bearish_hedge));

      if (c.is_bearish_hedge) hedgeCandidatesCount += 1;

      if (c.reached_proposed_order) {
        proposedTotal += 1;
        bucketDirection(proposedDirectionCounts, c.direction);
        bucketDirection(proposedExposureCounts, exposureDirection(c.direction, c.is_bearish_hedge));
        const action = c.proposed_action || "none";
        proposedActionCounts[action] = (proposedActionCounts[action] ?? 0) + 1;
      }
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
    proposedTotal,
    proposedDirectionCounts,
    proposedExposureCounts,
    proposedActionCounts,
  };
}

// A single -100..+100-shaped read of "which way is the signal leaning",
// derived from EXPOSURE-corrected counts (never raw instrument direction
// — see exposureDirection above, this is exactly the number its own
// comment says answers "is QAMC structurally long-only?"). All-bearish
// scores -100, all-bullish scores +100, balanced or all-neutral scores 0.
function netLean(counts: DirectionCounts, total: number): number {
  if (total === 0) return 0;
  return Math.round(((counts.bullish - counts.bearish) / total) * 100);
}

const DIRECTION_TONE: Record<"bullish" | "bearish" | "neutral", { label: string; color: Color; dot: string; text: string }> = {
  bullish: { label: "Long-leaning (bullish signal)", color: "emerald", dot: "bg-pos", text: "text-pos" },
  bearish: { label: "Short-leaning (bearish signal)", color: "rose", dot: "bg-neg", text: "text-neg" },
  neutral: { label: "Neutral / no signal", color: "slate", dot: "bg-dim", text: "text-dim" },
};

// Item 3 of the cockpit trader rework: this panel used to present the
// candidates-considered/proposed/hedge/risk/outcome breakdown as five
// bordered sub-sections stacked with hand-drawn ratio bars — "instrument
// signal direction, effective market exposure" as dense paragraphs of
// text the owner said he couldn't scan at a glance. He asked for the same
// underlying facts (long vs short vs cash share of exposure, and a single
// net-lean read) as standard chart components — Tremor's, the toolset
// already in this project — donuts specifically. No hand-drawn graphics:
// a previous panel was deleted for exactly that (see componentPolicy.test.ts's
// list of banned chart/table implementations this codebase has explicitly
// ruled out).
//
// What changed vs. the old five-section layout:
//  - "Candidates considered — direction" collapses into ONE donut plus a
//    BadgeDelta net-lean chip, both built on the EXPOSURE-corrected counts
//    (not the raw instrument-signal counts) — that's the number that
//    actually answers "is the system leaning long or short", the raw
//    instrument split is folded into one footnote sentence instead of a
//    second near-identical donut.
//  - "PM proposals" keeps its own (smaller-sample) exposure split and
//    action pills, but as one compact line instead of a second pair of bars.
//  - "Inverse-ETF (bearish-hedge) consideration" (a 4-stat-card grid) is
//    DROPPED. On live data it's 3 of 25 runs / 5 of 323 candidates — too
//    thin a sample to earn a dedicated section on an at-a-glance panel,
//    and the one fact worth keeping (hedge candidates exist and are
//    exposure-flipped) is already the one footnote sentence above.
//  - "AI Risk Manager verdicts, by run" is DROPPED outright. It answers
//    "how often does the risk gate approve", which is a different
//    question than directional bias, and on live data only 4 of 25 runs
//    ever reach a verdict — too thin to trust at this window size.
//  - "Outcome across window" (the executed/no-trade/blocked histogram) is
//    DROPPED outright. It isn't a directional read at all, and it
//    duplicates the Runs tab one click away in this same workspace group.
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

  const donutData = agg
    ? (["bullish", "bearish", "neutral"] as const)
        .map((key) => {
          const tone = DIRECTION_TONE[key];
          const value = key === "neutral" ? agg.candidateExposureCounts.neutralOrUnknown : agg.candidateExposureCounts[key];
          return { key, label: tone.label, value, color: tone.color, dot: tone.dot, text: tone.text };
        })
        .filter((d) => d.value > 0)
    : [];

  const lean = agg ? netLean(agg.candidateExposureCounts, agg.totalCandidates) : 0;
  const leanDeltaType = lean > 0 ? "increase" : lean < 0 ? "decrease" : "unchanged";
  const leanText =
    lean === 0
      ? "Balanced"
      : `${lean > 0 ? "+" : ""}${lean} net ${lean > 0 ? "long-leaning" : "short-leaning"}`;

  return (
    <Panel
      title="Directional Bias"
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
        <div className="flex flex-col gap-4">
          <div className="text-dim text-[0.72rem]">
            Window: last {agg.runsIncluded} of up to {AGGREGATION_WINDOW_RUNS} runs requested
            {agg.runsFetchFailed > 0 ? ` (${agg.runsFetchFailed} run${agg.runsFetchFailed === 1 ? "" : "s"} failed to load and were excluded)` : ""}.
          </div>

          {agg.totalCandidates === 0 ? (
            <StateMessage text="No candidates were considered in this window." />
          ) : (
            <>
              {/* Net directional lean — one number, one glance. Built on
                  exposure-corrected counts (see netLean above), never the
                  raw instrument signal. */}
              <div className="flex flex-wrap items-center gap-2.5">
                <BadgeDelta deltaType={leanDeltaType} size="lg">
                  {leanText}
                </BadgeDelta>
                <span className="text-dim text-[0.75rem]">
                  across {agg.totalCandidates} candidate consideration{agg.totalCandidates === 1 ? "" : "s"} this window
                </span>
              </div>

              {/* Long vs short vs neutral share of exposure — the donut. */}
              <div className="flex flex-wrap items-center gap-5">
                <DonutChart
                  data={donutData}
                  category="value"
                  index="label"
                  colors={donutData.map((d) => d.color)}
                  variant="donut"
                  showAnimation={false}
                  showTooltip
                  className="h-36 w-36 shrink-0"
                  valueFormatter={(v) => `${v}`}
                />
                <div className="flex flex-col gap-1.5 text-[0.78rem] min-w-[180px]">
                  {donutData.map((d) => (
                    <div key={d.key} className="flex items-center gap-1.5">
                      <span className={`inline-block h-2 w-2 rounded-full shrink-0 ${d.dot}`} aria-hidden="true" />
                      <span>{d.label}</span>
                      <span className="text-dim tabular-nums ml-auto pl-2">
                        {d.value} ({((d.value / agg.totalCandidates) * 100).toFixed(0)}%)
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              {agg.hedgeCandidatesCount > 0 && (
                <div className="text-dim text-[0.72rem]">
                  {agg.hedgeCandidatesCount} of {agg.totalCandidates} candidates ({agg.hedgeRunsConsidered} run
                  {agg.hedgeRunsConsidered === 1 ? "" : "s"}) were an inverse-ETF hedge — already reflected in the
                  exposure figures above, since a bullish signal on those expresses bearish exposure (and vice versa).
                </div>
              )}
            </>
          )}

          {/* PM proposals — the smaller-sample subset that actually reached
              a proposed order. Kept as one compact line + pills rather than
              a second pair of bars/donut: at this sample size (13 on live
              data vs. 323 considered) a second full chart would mostly be
              chart chrome around a handful of candidates. */}
          <div className="border-t border-border pt-3">
            <div className="text-[0.75rem] uppercase tracking-wide text-dim mb-1.5">PM proposals</div>
            {agg.proposedTotal === 0 ? (
              <StateMessage text="No candidates reached a proposed order in this window." />
            ) : (
              <div className="flex flex-col gap-1.5">
                <div className="text-[0.8rem]">
                  Of {agg.proposedTotal} candidate{agg.proposedTotal === 1 ? "" : "s"} that reached a proposed order:{" "}
                  <span className="text-pos font-semibold">{agg.proposedExposureCounts.bullish} long-leaning</span>,{" "}
                  <span className="text-neg font-semibold">{agg.proposedExposureCounts.bearish} short-leaning</span>,{" "}
                  <span className="text-dim">{agg.proposedExposureCounts.neutralOrUnknown} neutral</span>.
                </div>
                <div className="flex flex-wrap gap-2 items-center">
                  <span className="text-dim text-[0.72rem]">Actions:</span>
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
        </div>
      )}
    </Panel>
  );
}

// Exported for unit testing (DirectionalBiasPanel.test.ts) — the pure
// aggregation/derivation logic, kept separate from data-fetching and
// rendering so it's directly verifiable without mounting a component.
export { exposureDirection, computeAggregates, netLean };
export type { Aggregates, DirectionCounts };
